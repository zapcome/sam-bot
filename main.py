'''
sam-bot

You need to have an Event subscription enabled that points to the flask app
https://api.slack.com/apps/A012QE04RME/event-subscriptions?
https://api.slack.com/events-api#subscriptions
https://github.com/slackapi/python-slack-events-api
https://github.com/slackapi/python-slackclient/blob/master/tutorial/03-responding-to-slack-events.md
'''

import logging
from logging.config import dictConfig
import json
import os
import sys
import time
import traceback
import threading

import git
import requests
import flask
import slack
import slack.errors
from slackeventsapi import SlackEventAdapter
from mispattruploader import MispCustomConnector

dir_path = os.path.dirname(os.path.realpath(__file__))
config_file = dir_path + '/config.json'

# parse config file
with open(config_file) as json_data_file:
    try:
        data = json.load(json_data_file)
    except json.decoder.JSONDecodeError as error:
        sys.exit(f"Couldn't parse config.json: {error}")

if 'testing' in data:
    TEST_MODE = data.get('testing')
else:
    TEST_MODE = os.getenv('TEST_MODE', False)

if 'logging' in data:
    # default to sambot.log in log dir next to script if it's not set
    LOGFILE_DEFAULT = data['logging'].get('output_file', f"{dir_path}/logs/sambot.log")
    # default to sambot_error.log in log dir next to script if it's not set
    LOGFILE_ERROR = data['logging'].get('output_error_file', f"{dir_path}/logs/sambot_error.log")
else:
    # defaults
    LOGFILE_DEFAULT = "./logs/sambot.log"
    LOGFILE_ERROR = "./logs/sambot_error.log"

logging_config = dict(
    version = 1,
    formatters = {
        'f': {'format':
              '%(asctime)s - %(name)s - %(levelname)s - %(message)s'}
        },
    handlers = {
        'Stream': {'class': 'logging.StreamHandler',
              'formatter': 'f',
              'level': 'DEBUG'
        },
        'file_all': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'DEBUG',
            'formatter': 'f',
            'filename': LOGFILE_DEFAULT,
            'mode': 'a',
            'maxBytes': 10485760,
            'backupCount': 5,
        },
    },
    root = {
        'handlers': ['Stream', 'file_all'],
        'level': 'DEBUG',
        },
)


logging_config['handlers']['file_error'] = {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'ERROR',
            'formatter': 'f',
            'filename': LOGFILE_ERROR,
            'mode': 'a',
            'maxBytes': 10485760,
            'backupCount': 5,
    }
logging_config['root']['handlers'].append('file_error')

dictConfig(logging_config)

logger = logging.getLogger('SAMbot')

# connecting to MISP
try:
    misp = MispCustomConnector(misp_url=data['misp']['url'],
                       misp_key=data['misp']['key'],
                       misp_ssl=data.get('misp', {}).get('ssl', True), # default to using SSL
                       )
    logger.info("Connected to misp server successfully")
# Who knows what kind of errors PyMISP will throw?
#pylint: disable=broad-except
except Exception:
    logger.error('Failed to connect to MISP:')
    logger.error(traceback.format_exc())
    sys.exit(1)

# config file - slack section
if not data.get('slack'):
    logger.error("No 'slack' config section, quitting.")
    sys.exit(1)

MISSED_SLACK_KEY = False
for key in ('SLACK_BOT_OAUTH_TOKEN', 'SLACK_SIGNING_SECRET'):
    if key not in data.get('slack'):
        MISSED_SLACK_KEY = True
        logger.error("Couldn't find %s in config.json slack section, going to quit.", key)
if MISSED_SLACK_KEY:
    sys.exit(1)
else:
    slack_bot_token = data['slack']['SLACK_BOT_OAUTH_TOKEN']
    slack_signing_secret = data['slack']['SLACK_SIGNING_SECRET']


slack_events_adapter = SlackEventAdapter(slack_signing_secret, '/slack/events')

def get_username(prog_username, slack_client, token):
    """ pulls the slack username from the event """
    logger.debug('Got %s as username', prog_username)
    user_info = slack_client.users_info(token=token, user=prog_username)
    if user_info.get('ok'):
        if user_info.get('user') is not None:
            user = user_info['user']
            if user.get('profile') is not None:
                profile = user['profile']
                if profile.get('display_name') is not None:
                    username = profile['display_name']
                    logger.debug('Returning %s', username)
                    return username
    return False

def file_handler(event):
    """ handles files from slack client """
    logger.info('got file from slack')

    for file_object in event.get('files'):
        if file_object.get('mode') == "snippet":
            url = file_object.get('url_private_download')
            title = file_object.get('title')
            if title == 'Untitled':
                event_title = '#Warroom'
            else:
                event_title = f"#Warroom {title}"
            headers = {'Authorization': f"Bearer {slack_bot_token}"}

            response = requests.get(url, headers=headers)
            response.raise_for_status()

            # TODO: this might just need to be response.text
            content = response.content.decode("utf-8")

            e_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(event['event_ts'])))
            e_title = f"{e_time} - {event_title}"
            username = get_username(event.get('user'), slack_client, slack_bot_token)
            logger.info(username)
            logger.info(e_title)
            logger.info(content)
            misp_response = misp.misp_send(0, content, e_title, username)
            slack_client.chat_postEphemeral(
                channel=event.get('channel'),
                text=misp_response,
                user=event.get('user'),
            )

@slack_events_adapter.on('message')
def handle_message(event_data):
    """ slack message handler """
    logger.info('handle_message Got message from slack')
    logger.info(event_data)
    message = event_data.get('event')

    logger.info(f"Message type: {message.get('type')}")
    logger.info(f"Message text: {message.get('text')}")
    if message.get('files'):
        logger.info("Files message")
        file_info = message
        thread_object = threading.Thread(target=file_handler, args=(file_info,))
        thread_object.start()
        #file_handler(file_info)
        return_value = flask.Response('', headers={'X-Slack-No-Retry': 1}), 200
    # elif str(message.get('type')) == 'message' and str(message.get('text')) == 'sambot git update':
    #     logger.info(f"Git pull message from {message.get('user')} in {message.get('channel')}")

    #     response = f"Doing a git pull now..."
    #     slack_client.chat_postMessage(channel=message.get('channel'), text=response)

    #     git_repo = git.cmd.Git(os.path.dirname(os.path.realpath(__file__)))
    #     git_result = git_repo.pull()

    #     response = f"Done!\n```{git_result}```"
    #     slack_client.chat_postMessage(channel=message.get('channel'), text=response)

    #     return_value = '', 200
    # if the incoming message contains 'hi', then respond with a 'hello message'
    elif message.get('subtype') is None and 'hi' in message.get('text'):
        logger.info(f"Hi message from {message.get('user')} in {message.get('channel')}")
        response = f"Hello <@{message.get('user')}>! :tada:"
        slack_client.chat_postMessage(channel=message.get('channel'), text=response)
        return_value = '', 200
    else:
        logger.info("Message fell through...")
    # shouldn't get here, but return a 403 if you do.
    return_value = 'Unhandled message type', 403
    return return_value

@slack_events_adapter.on("error")
def error_handler(err):
    """ slack error message handler """
    logger.error("Slack error: %s", str(err))

def find_channel_id(slack_client, channel_name='_autobot'):
    """ returns the channel ID of the channel """
    for channel in slack_client.conversations_list().get('channels'):
        if channel.get('name') == channel_name:
            logger.debug(f"found channel id for {channel_name}: {channel.get('id')}")
            return channel.get('id')
    return False

if __name__ == '__main__':
    slack_client = slack.WebClient(slack_bot_token)
    
    if TEST_MODE:
        BOT_CHANNEL = find_channel_id(slack_client, '_autobot')
        slack_client.conversations_join(channel=BOT_CHANNEL)
        #slack_client.chat_postMessage(channel=BOT_CHANNEL, text="I've starting up in test mode!")
        logger.debug("I've started up in test mode...")

    for channel in slack_client.conversations_list().get('channels'):
        logger.debug(channel)
    slack_events_adapter.start(port=3000, host='0.0.0.0')
