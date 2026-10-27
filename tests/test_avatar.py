import asyncio
import os
import unittest
from unittest.mock import patch
from avatar.config import Settings
from avatar.service import Trial, TrialError

TOKEN = 'offline_only_A9xB7yC5zD3wE1vF8uG6tH4sJ2rK0qM'


class Store:
    values = {}
    def __init__(self,*_,**__): pass
    def read(self,key): return self.values.get(key)
    def alive(self): pass
    def close(self): pass
    def write(self,key,value,*,reserve=False):
        if reserve and key in self.values: raise ValueError('already_reserved')
        self.values[key] = dict(value)


class AvatarTests(unittest.IsolatedAsyncioTestCase):
    async def test_paid_operations_disabled_before_any_sdk_work(self):
        with patch.dict(os.environ,{},clear=True): trial = Trial(Settings(owner_key=TOKEN))
        with self.assertRaises(TrialError) as error: await trial.start()
        self.assertEqual(error.exception.code,'real_sessions_disabled')
        self.assertIsNone(trial.gateway)

    async def test_reservation_survives_restart_and_never_replays_create(self):
        Store.values = {}
        with patch.dict(os.environ,{'DATABASE_URL':'synthetic','STATE_NAMESPACE':'synthetic'}),patch('cloud.store.DurableStore',Store):
            trial = Trial(Settings(owner_key=TOKEN))
            trial.record = {'status':'starting','id':'a'*32,'room':'avatar-lk-'+'a'*32,'expires':100,'pending_create':'room'}
            trial.save(reserve=True)
            restarted = Trial(Settings(owner_key=TOKEN))
            self.assertTrue(restarted.spent)
            self.assertEqual(restarted.record['status'],'reconcile_required')
            self.assertTrue(restarted.record['uncertain'])
            with self.assertRaises(Exception): restarted.save(reserve=True)

    def test_budget_cannot_exceed_the_confirmed_trial(self):
        with self.assertRaises(ValueError): Settings(owner_key=TOKEN,duration=101)
