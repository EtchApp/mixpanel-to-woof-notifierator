#!/usr/bin/env python
"""MixPanel New User Notifierator"""
from __future__ import division

import base64
import logging
import time
import urllib2

from google.cloud import storage
from google.appengine.api import app_identity
from google.appengine.api import mail
from google.appengine.api import urlfetch
import googleapiclient.discovery

from flask import Flask
from mixpanel import Mixpanel
from twilio.rest import Client


BUCKET              = '...'                     # noqa: E221
KMS_LOCATION        = 'global'                  # noqa: E221
KMS_KEYRING         = '...'                     # noqa: E221
MIXPANEL_CRYPTOKEY  = '...'                     # noqa: E221
MIXPANEL_API_FILE   = 'mixpanel.encrypted'      # noqa: E221
TWILIO_CRYPTOKEY    = '...'                     # noqa: E221
TWILIO_API_FILE     = 'twilio.encrypted'        # noqa: E221

RUNTIME             = 60                        # noqa: E221
TO_ADDRESS          = 'contact@foobar.com'      # noqa: E221
SUBJECT             = '[app name] new users'    # noqa: E221
APP_SMS_PREFIX      = '[app name] '             # noqa: E221
FROM_PHONE          = '+1..........'            # noqa: E221
EXCLUDE_NAMES       = ['...',                   # noqa: E221
                       '...']
TO_PHONES           = ['+1..........',          # noqa: E221
                       '+1..........',
                       '+1..........']


urlfetch.set_default_fetch_deadline(45)
app = Flask(__name__)


def _decrypt(project_id, location, keyring, cryptokey, cipher_text):
    """Decrypts and returns string from given cipher text."""
    logging.info('Decrypting cryptokey: {}'.format(cryptokey))
    kms_client = googleapiclient.discovery.build('cloudkms', 'v1')
    name = 'projects/{}/locations/{}/keyRings/{}/cryptoKeys/{}'.format(
        project_id, location, keyring, cryptokey)
    cryptokeys = kms_client.projects().locations().keyRings().cryptoKeys()
    request = cryptokeys.decrypt(
        name=name,
        body={'ciphertext': base64.b64encode(cipher_text).decode('ascii')})
    response = request.execute()
    return base64.b64decode(response['plaintext'].encode('ascii'))


def _download_output(output_bucket, filename):
    """Downloads the output file from GCS and returns it as a string."""
    logging.info('Downloading output file')
    client = storage.Client()
    bucket = client.get_bucket(output_bucket)
    output_blob = (
        'keys/{}'
        .format(filename))
    return bucket.blob(output_blob).download_as_string()


def get_credentials(cryptokey, filename):
    """Fetches credentials from KMS returning a decrypted API key."""
    credentials_enc = _download_output(BUCKET, filename)
    credentials_dec = _decrypt(app_identity.get_application_id(),
                               KMS_LOCATION,
                               KMS_KEYRING,
                               cryptokey,
                               credentials_enc)
    return credentials_dec


def get_new_users(key, time_in_minutes):
    """Gets new users from MixPanel."""
    logging.info('Making an API call to MixPanel')
    api = Mixpanel(api_secret=str(key).strip())
    mixpanel_data = {}

    current_time = int(time.time())
    time_in_seconds = int(float(time_in_minutes)*60)
    created_start_search = 'datetime({0})'.format(
        current_time - time_in_seconds - RUNTIME)
    try:
        mixpanel_data = api.request(['engage'],
                                    {'where': '{0} < user["$created"]'.format(
                                        created_start_search)})
        # '(properties["$created"]) > XXXX-YY-ZZ'
    except (urllib2.URLError, urllib2.HTTPError) as error:
        logging.exception('An error occurred: {0}'.format(error))

    # Pagination with weird MixPanel API
    session_id = mixpanel_data['session_id']  # Unsure if it stays the same
    current_page = mixpanel_data['page']
    current_total = mixpanel_data['total']
    while current_total >= 1000:
        logging.info('Page: {0}'.format(current_page + 1))
        try:
            mixpanel_data['results'].append(api.request(['engage'], {
                'page': current_page + 1,
                'session_id': session_id
            })['results'])
        except (urllib2.URLError, urllib2.HTTPError) as error:
            logging.error('An error occurred: {0}'.format(error))
            pass

    return mixpanel_data


def cleanup_mixpanel_data(results):
    """Cleans up the MixPanel data."""
    cleaned_up_data = {}

    for user in results['results']:
        try:
            if user['$properties']['$name'] not in EXCLUDE_NAMES:
                device_model = user['$properties'].get('$ios_device_model', 'Unknown')           # noqa: E501
                device_version = user['$properties'].get('$ios_version', 'Unknown')              # noqa: E501
                cleaned_up_data[user['$properties']['$email']] = {
                    'name': user['$properties']['$name'],
                    'device': 'Device: {0}, Running: {1}'.format(device_model, device_version),  # noqa: E501
                }
        # Missing values are entirely possible, this is analytics data!
        except (KeyError, ValueError) as error:
            logging.error('An error occurred cleaning up data: {0}'.format(error))               # noqa: E501
            logging.error('User data: {0}'.format(user))
            pass

    return cleaned_up_data


def send_mail(new_users, time_formatted):
    """Send mail with list of new users."""
    message_body = 'New users for the last {0}\n\n'.format(time_formatted)
    for email, full_name, device in new_users.iteritems():
        message_body = '{0}Name: {1}\nEmail: {2}\nDevice: {3}\n\n'.format(
            message_body, full_name, email, device)

    message = mail.EmailMessage(
        sender='contact@{0}.appspotmail.com'.format(
            app_identity.get_application_id()),
        subject=SUBJECT)

    message.to = TO_ADDRESS
    message.body = message_body
    message.send()
    logging.info('Mail Sent')


def send_sms(creds, new_users_count, time_formatted):
    """Send SMS with list of new users."""
    # Assumes credentials are comma-separated account sid and auth token
    creds = str(creds).strip().split(',')
    client = Client(creds[0], creds[1])

    for phone in TO_PHONES:
        message = client.messages.create(
            to=phone,
            from_=FROM_PHONE,
            body="{0} new users: {1} new user(s) over the past {2}!".format(
                APP_SMS_PREFIX, new_users_count, time_formatted))

        logging.info('SMS Sent with SID: {0}'.format(message.sid))


def runit(time_in_minutes):
    """Runs the task."""
    mixpanel_creds = get_credentials(MIXPANEL_CRYPTOKEY, MIXPANEL_API_FILE)
    twilio_creds = get_credentials(TWILIO_CRYPTOKEY, TWILIO_API_FILE)

    new_users = get_new_users(mixpanel_creds, time_in_minutes)
    new_users_formatted = cleanup_mixpanel_data(new_users)
    new_users_count = len(new_users_formatted)

    if new_users_count >= 1:
        logging.info('{0} new users'.format(new_users_count))

        if float(time_in_minutes) > 59:
            time_formatted = '{0}hr(s)'.format(int(time_in_minutes) / 60)
        else:
            time_formatted = '{0}min(s)'.format(time_in_minutes)

        send_mail(new_users_formatted, time_formatted)
        send_sms(twilio_creds, new_users_count, time_formatted)
    else:
        logging.info('No new users')

    return 'Completed'


@app.route('/run/<time_in_minutes>')
def run(time_in_minutes):
    if not time_in_minutes:
        return 'Must specify run time in minutes!'
    else:
        return runit(time_in_minutes)


@app.errorhandler(500)
def server_error(e):
    # Log the error and stacktrace.
    logging.exception('An error occurred during a request.')
    return 'An internal error occurred.', 500
