"""Finite-state-machine states for multi-step admin flows."""
from aiogram.fsm.state import State, StatesGroup


class AddChannelFlow(StatesGroup):
    role = State()
    chat_id = State()


class SetCursorFlow(StatesGroup):
    waiting_id = State()


class BroadcastFlow(StatesGroup):
    waiting_text = State()


class ScheduleFlow(StatesGroup):
    waiting_duration = State()
