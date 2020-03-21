import logging
import random

import requests
import vk_api
from pony.orm import db_session
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
import handlers
from models import UserState, Registration

try:
    import settings
except ImportError:
    exit('DO cp settings.py.default settings.py and set token!')

log = logging.getLogger('bot')


def configure_logging():
    stream_handler = logging.StreamHandler()
    log.addHandler(stream_handler)
    stream_handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    stream_handler.setLevel(logging.INFO)

    file_handler = logging.FileHandler('bot.log')
    log.addHandler(file_handler)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%m-%d-%Y %I:%M %p'))
    log.setLevel(logging.DEBUG)


class Bot:
    """
    Сценарий регистрации на конференцию через vk.com.
    Use python3.8

    Поддерживает ответы на вопросы про дату, место проведения и сценарий регистрации:
    - спрашиваем имя
    - спрашиваем email
    - говорим об успешной регистрации
    Если шаг не пройденЮ задаем уточняющий вопрос пока шаг не будет пройден.
    """

    def __init__(self, group_id, token):
        """
        :param group_id: group id in vk group
        :param token: secret token
        """
        self.group_id = group_id
        self.token = token
        self.vk = vk_api.VkApi(token=token)
        self.long_poller = VkBotLongPoll(self.vk, self.group_id)
        self.api = self.vk.get_api()

    def run(self):
        """Run bot."""
        for event in self.long_poller.listen():
            try:
                self.on_event(event)
            except Exception:
                logging.exception('ошибка в обработке события')

    @db_session
    def on_event(self, event):
        """
        Send message back if it's text.
        :param event: VkBotMessageEvent object
        :return: None
        """
        if event.type != VkBotEventType.MESSAGE_NEW:
            log.info('мы пока не умеем обрабатывать событие такого типа %s', event.type)
            return

        user_id = event.object.message['peer_id']
        text = event.object.message['text']
        state = UserState.get(user_id=str(user_id))

        if state is not None:
            self.continue_scenario(text, state, user_id)
        else:
            # search_intend
            for intent in settings.INTENTS:
                log.debug(f'User gets {intent}')
                if any(token in text.lower() for token in intent['tokens']):
                    if intent['answer']:
                        self.send_text(intent['answer'], user_id)
                    else:
                        self.start_scenario(user_id, intent['scenario'], text)
                    break
            else:
                self.send_text(settings.DEFAULT_ANSWER, user_id)

    def send_text(self, text_to_send, user_id):
        self.api.messages.send(
            message=text_to_send,
            random_id=random.randint(0, 2 ** 20),
            peer_id=user_id,
        )

    def send_image(self, image, user_id):
        upload_url = self.api.photos.getMessagesUploadServer()['upload_url']
        upload_data = requests.post(url=upload_url, files={'photo': ('image.png', image, 'image/png')}).json()
        image_data = self.api.photos.saveMessagesPhoto(**upload_data)

        owner_id = image_data[0]['owner_id']
        media_id = image_data[0]['id']
        attachment = f'photo{owner_id}_{media_id}'

        self.api.messages.send(
            attachment=attachment,
            random_id=random.randint(0, 2 ** 20),
            peer_id=user_id,
        )

    def send_step(self, step, user_id, text, context):
        if 'text' in step:
            self.send_text(step['text'].format(**context), user_id)
        if 'image' in step:
            handler = getattr(handlers, step['image'])
            image = handler(text, context)
            self.send_image(image, user_id)

    def start_scenario(self, user_id, scenario_name, text):
        scenario = settings.SCENARIOS[scenario_name]
        first_step = scenario['first_step']
        step = scenario['steps'][first_step]
        self.send_step(step, user_id, text, context={})
        UserState(user_id=str(user_id), scenario_name=scenario_name, step_name=first_step, context={})

    def continue_scenario(self, text, state, user_id):
        steps = settings.SCENARIOS[state.scenario_name]['steps']
        step = steps[state.step_name]

        handler = getattr(handlers, step['handler'])
        if handler(text=text, context=state.context):
            # next step
            next_step = steps[step['next_step']]
            self.send_step(next_step, user_id, text, state.context)

            if next_step['next_step']:
                # switch to next step
                state.step_name = step['next_step']
            else:
                # finish scenario
                log.info('Зарегистрирован: {name} {email}'.format(**state.context))
                Registration(name=state.context['name'], email=state.context['email'])
                state.delete()
        else:
            # retry current step
            text_to_send = step['failure_text'].format(**state.context)
            self.send_text(text_to_send, user_id)


if __name__ == '__main__':
    configure_logging()
    bot = Bot(settings.GROUP_ID, settings.TOKEN)
    bot.run()
