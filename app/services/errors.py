"""Общее исключение сервисов с человекочитаемым сообщением для Telegram."""


class ServiceError(Exception):
    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message
