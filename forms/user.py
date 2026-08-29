from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    name = State()
    edit_name = State()
    sms = State()
    password = State()
    pin = State()

    waiting_owner_action = State()
    confirm_pin = State()

class Registration(StatesGroup):
    name = State()
    phone = State()
    sms = State()
    waiting_permission = State()


# ИЗМЕНЕНО: CategoryEditing, LimitEditing и Planning убраны - вся работа с
# категориями, лимитами (планом трат по месяцам) и ЗП теперь происходит в
# Mini App "План на год" (webapp/plan.html, services/webapp_api.py) без
# участия чатового FSM-сценария. Точечные команды /зп и /план
# (handlers/planning.py) по-прежнему работают без отдельного состояния -
# это однострочные команды с аргументами, а не пошаговый диалог.
