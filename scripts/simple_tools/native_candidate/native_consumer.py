"""Bounded future evidence sweep candidate. Never dispatches, never edits router receipts.

Owner provides live binding verifier. Scheduling/install remains explicitly separate.
Each step takes frozen journal inputs, queries existing commands only, and reads the
owning native store through Driver.reconcile_only. Evidence has private durable custody.
"""
import re,time
from pathlib import Path
from .native_custody import read_private
from .zcode_native import NativeDelivery

def projection(record):
    e=record.get('evidence',{})
    return {'wake_key':record['wake_key'],'binding':record['binding'],
        'admission':e.get('admission','unverified'),'native_input':e.get('native_input','unverified'),'turn':e.get('turn','unverified'),
        'owning_alert':e.get('owning_alert','unverified'),'handling':'unverified',
        'source_refs':{k:e[k]for k in ('message_id','sourceCommandId','alert_id','fetch_message_id','fetch_part_id')if k in e},
        'conflict':bool(e.get('conflict') or record.get('conflict')),
        'read_incomplete':bool(record.get('read_incomplete'))}

class Consumer:
    def __init__(self,driver,verify,max_records=32):
        if type(max_records) is not int or not 1<=max_records<=128:raise ValueError('consumer-budget')
        self.driver,self.verify,self.max_records=driver,verify,max_records
        self.after=''
    def step(self):
        folder=self.driver.state/'native-commands'
        names=sorted(p.name for p in folder.glob('*.json')if re.fullmatch(r'[a-f0-9]{64}\.json',p.name))
        available=[n for n in names if n>self.after]
        if not available:self.after='';available=names
        reports=[]
        for name in available[:self.max_records]:
            self.after=name
            try:
                original=read_private(folder/name)
                command_id='app-control-echo-review-'+name[:-5]
                if not NativeDelivery._valid_record(original,command_id):raise ValueError('consumer-journal-invalid')
                binding=original['binding'];intent=original['intent']
                if binding.get('host')!='server' or binding.get('local') is not True:raise ValueError('consumer-local-only')
                if not self.verify(binding):raise ValueError('consumer-binding-changed')
                result=self.driver.reconcile_only(binding,intent['wake_key'],intent['text'],verify=self.verify)
                if result.get('evidence_persistence')=='unverified':raise ValueError('consumer-evidence-not-saved')
                evidence_path=self.driver.state/'native-evidence'/name
                if not self.verify(binding):raise ValueError('consumer-binding-changed')
                evidence=read_private(evidence_path)
                if evidence['binding']!=binding or evidence['wake_key']!=intent['wake_key']:raise ValueError('consumer-evidence-binding')
                if not self.verify(binding):raise ValueError('consumer-post-read-binding-changed')
                report=projection(evidence)
                report['query_code']=result.get('code','unverified')
                report['observed_at']=time.time()
                reports.append(report)
            except Exception:
                reports.append({'command_id': 'app-control-echo-review-'+name[:-5], 'reconciliation':'unverified','handling':'unverified'})
        return reports

    def run(self,emit,*,max_rounds=12,interval_seconds=300,wait=time.sleep):
        # Explicit bounded worker entry for later scheduler wiring. Nothing starts
        # at import or installation, and no dispatch code exists in this worker.
        if type(max_rounds) is not int or not 1<=max_rounds<=288 or interval_seconds<60:raise ValueError('consumer-run-budget')
        for index in range(max_rounds):
            emit(self.step())
            if index+1<max_rounds:wait(interval_seconds)
