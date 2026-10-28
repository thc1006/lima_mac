"""Offline transaction fault fixtures. No native adapter is invoked on a host."""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from supervisor_install import INITIAL_SERVICE, InstallOperations, MaintenanceTermination, RecoveryError, coordinated_install


class Facts:
    def __init__(self, failure=None, *, helper=False, secondary=None):
        self.failure=failure;self.secondary=secondary;self.events=[];self.locked=False
        self.marker=None;self.files=['old','old-helper' if helper else None]
        self.original_files=list(self.files);self.service=self.control('old',1872)
        self.records={};self.foreign=False;self.drift=False;self.stage_cleaned=False
        self.clock=2000;self.wrote_new=False

    @staticmethod
    def control(kind,pid):
        return {'kind':kind,'service':{'pid':pid,'start':'start-'+str(pid)}}

    def event(self,name):
        assert self.locked
        self.events.append(name)
        if self.failure==name:
            self.failure=None
            raise RecoveryError(name)

    @contextmanager
    def lock(self):
        if self.failure=='lock':raise RecoveryError('lock_busy')
        self.locked=True
        try:yield
        finally:self.locked=False

    def prepare(self):
        self.event('prepare')
        if self.marker is not None:raise RecoveryError('foreign_drain')
        return {'control':self.service,'old_pair':list(self.files),'VM_states':{'golden':'Stopped'}}

    def record(self,name,value):
        self.event('record:'+name)
        if self.marker and (self.secondary=='ENOSPC' or self.secondary=='ENOSPC-after-pair' and self.wrote_new):
            raise OSError(errno.ENOSPC,'fixture_full')
        if name in self.records:raise FileExistsError(name)
        self.records[name]=repr(value)

    def create_owned_drain(self,proof):
        self.event('create-drain')
        self.marker='owned';proof['marker_identity']='owned'
        self.event('drain-created')

    def marker_present(self):return self.marker is not None

    def verify_boundary(self,proof):
        self.event('boundary')
        if self.marker!='owned' or self.foreign or self.drift:raise RecoveryError('identity_changed')

    def verify(self,proof,kind,control):
        self.verify_boundary(proof);self.event('verify:'+kind)
        if control!=self.service:raise RecoveryError('process_identity_changed')
        if kind=='old' and self.files!=self.original_files:raise RecoveryError('old_pair_mismatch')
        if kind=='new' and self.files!=['new','new-helper']:raise RecoveryError('new_pair_mismatch')

    def wait_idle(self,control,timeout):
        self.event('idle:'+control['kind'])

    def bootout(self,control):
        self.event('bootout')
        assert self.marker=='owned' and self.service==control
        self.service=None;self.event('bootout-stopped')

    def install_pair(self,kind):
        self.event('install:'+kind)
        assert self.marker=='owned' and self.service is None
        self.files[0]='new' if kind=='new' else 'old';self.wrote_new=kind=='new'
        if self.secondary in ('foreign-marker','storage-drift','foreign-file'):
            if self.secondary=='foreign-marker':self.marker='foreign'
            elif self.secondary=='storage-drift':self.drift=True
            else:self.foreign=True
            raise RecoveryError('fixture_drift')
        if self.secondary=='TERM':raise MaintenanceTermination(signal.SIGTERM)
        if self.secondary=='HUP':raise MaintenanceTermination(signal.SIGHUP)
        if self.secondary=='INT':raise KeyboardInterrupt()
        if self.secondary=='EXIT17':raise SystemExit(17)
        self.event('half-pair')
        self.files[:]=['new','new-helper'] if kind=='new' else self.original_files

    def bootstrap(self,kind,proof):
        self.event('bootstrap:'+kind)
        assert self.marker=='owned' and self.service is None
        self.clock+=1;self.service=self.control(kind,self.clock)
        self.event('bootstrapped:'+kind)
        return self.service

    def restore_old(self,proof):
        self.verify_boundary(proof);self.event('restore')
        if self.secondary=='foreign-service':raise RecoveryError('unknown_service')
        if self.service==proof['control'] and self.files==self.original_files:return self.service
        self.service=None;self.files[:]=self.original_files
        if self.secondary=='rollback-bootstrap-fail':raise RecoveryError('old_bootstrap_failed')
        return self.bootstrap('old',proof)

    def cleanup_stages(self):
        self.event('cleanup-stages');self.stage_cleaned=True

    def release(self,proof,kind,control):
        self.verify(proof,kind,control);self.event('release:'+kind)
        self.marker=None


class SupervisorInstallTests(unittest.TestCase):
    def test_initial_source_bound_service_requires_the_reviewed_PID_start_and_GOLDEN(self):
        control = {'service': dict(INITIAL_SERVICE)}
        native = type('Native', (), {'_control': lambda self, kind: control})()
        self.assertEqual(InstallOperations._initial_control(native), control)
        for change in ({'pid': 1872}, {'pid': 99764}, {'start': 'Wed Oct 28 10:02:51 2026'}, {'GOLDEN': 'other'}):
            with self.subTest(change=change):
                control = {'service': {**INITIAL_SERVICE, **change}}
                with self.assertRaisesRegex(RecoveryError, 'original_live_identity_changed_requires_review'):
                    InstallOperations._initial_control(native)

    def recovered(self,facts,rc=1):
        result=coordinated_install(facts)
        self.assertEqual(result['return_code'],rc,result)
        self.assertTrue(result['recovered'],result)
        self.assertTrue(result['admission_restored'],result)
        self.assertEqual(facts.files,facts.original_files)
        self.assertIsNone(facts.marker)
        self.assertEqual(facts.service['kind'],'old')
        self.assertTrue(facts.stage_cleaned)
        return result

    def test_success_installs_whole_pair_before_bootstrap_and_releases_last(self):
        f=Facts();r=coordinated_install(f)
        self.assertEqual(r['return_code'],0);self.assertTrue(r['installed'])
        self.assertEqual(f.files,['new','new-helper']);self.assertIsNone(f.marker)
        for earlier,later in [('record:install-intent.json','create-drain'),('idle:old','bootout'),
                              ('half-pair','bootstrap:new'),('idle:new','release:new'),
                              ('cleanup-stages','release:new')]:
            self.assertLess(f.events.index(earlier),f.events.index(later))
        self.assertNotEqual(f.service['service']['pid'],1872)

    def test_lock_refusal_has_no_install_or_drain(self):
        f=Facts('lock');r=coordinated_install(f)
        self.assertNotEqual(r['return_code'],0);self.assertEqual(f.events,[]);self.assertIsNone(f.marker)

    def test_foreign_existing_drain_is_not_adopted_or_removed(self):
        f=Facts();f.marker='foreign';r=coordinated_install(f)
        self.assertEqual(f.marker,'foreign');self.assertNotIn('bootout',f.events);self.assertFalse(r['recovered'])

    def test_intent_failure_does_not_stop_service(self):
        f=Facts('record:install-intent.json');r=coordinated_install(f)
        self.assertEqual(r['return_code'],1);self.assertNotIn('bootout',f.events);self.assertIsNone(f.marker)

    def test_post_create_failure_recovers_captured_owned_marker(self):
        self.recovered(Facts('drain-created'))

    def test_bootout_failure_keeps_original_healthy_process(self):
        f=Facts('bootout');self.recovered(f)
        self.assertEqual(f.service['service']['pid'],1872)

    def test_bootout_nonzero_after_stop_restarts_old_with_fresh_identity(self):
        f=Facts('bootout-stopped');self.recovered(f)
        self.assertNotEqual(f.service['service']['pid'],1872)

    def test_half_pair_is_restored_before_old_bootstrap(self):
        f=Facts('half-pair');self.recovered(f)
        self.assertLess(f.events.index('restore'),f.events.index('bootstrap:old'))

    def test_absent_old_helper_is_restored_as_absent(self):
        f=Facts('idle:new');self.recovered(f)
        self.assertIsNone(f.files[1])

    def test_existing_old_helper_is_restored_with_old_pair(self):
        f=Facts('idle:new',helper=True);self.recovered(f)
        self.assertEqual(f.files[1],'old-helper')

    def test_new_bootstrap_failure_before_or_after_process_is_recovered(self):
        for failure in ('bootstrap:new','bootstrapped:new'):
            with self.subTest(failure=failure):self.recovered(Facts(failure))

    def test_new_service_health_failure_rolls_back_with_new_old_service_PID(self):
        f=Facts('idle:new');r=self.recovered(f)
        self.assertNotEqual(r['rollback_service']['pid'],1872)

    def test_rollback_bootstrap_failure_keeps_drain_and_nonzero(self):
        f=Facts('idle:new',secondary='rollback-bootstrap-fail');r=coordinated_install(f)
        self.assertEqual(r['return_code'],1);self.assertFalse(r['recovered']);self.assertEqual(f.marker,'owned')
        self.assertNotIn('release:old',f.events)

    def test_signals_interrupt_and_SystemExit_preserve_original_codes(self):
        for kind,rc in [('TERM',143),('HUP',129),('INT',130),('EXIT17',17)]:
            with self.subTest(kind=kind):self.recovered(Facts(secondary=kind),rc)

    def test_persistent_later_audit_ENOSPC_does_not_strand_original_healthy_control(self):
        for failure in ('ENOSPC','ENOSPC-after-pair'):
            with self.subTest(failure=failure):
                f=Facts(secondary=failure);r=self.recovered(f)
                self.assertTrue(r['evidence_unavailable'])

    def test_foreign_marker_storage_or_file_drift_cannot_release_admission(self):
        for kind in ('foreign-marker','storage-drift','foreign-file'):
            with self.subTest(kind=kind):
                f=Facts(secondary=kind);r=coordinated_install(f)
                self.assertNotEqual(r['return_code'],0);self.assertFalse(r['recovered'])
                self.assertIsNotNone(f.marker);self.assertNotIn('release:old',f.events)

    def test_unknown_replacement_process_is_not_stopped_for_rollback(self):
        f=Facts('idle:new',secondary='foreign-service');r=coordinated_install(f)
        self.assertFalse(r['recovered']);self.assertEqual(f.marker,'owned')

    def test_post_commit_audit_failure_is_nonzero_without_stopping_admitted_control(self):
        f=Facts('record:install-outcome.json');r=coordinated_install(f)
        self.assertEqual(r['return_code'],1);self.assertTrue(r['installed'])
        self.assertTrue(r['admission_restored']);self.assertTrue(r['post_commit_evidence_failure'])
        self.assertIsNone(f.marker);self.assertEqual(f.service['kind'],'new')
        self.assertNotIn('restore',f.events)

    def test_stale_incomplete_or_busy_queue_envelopes_are_rejected(self):
        valid={'complete':True,'repository':'nycu-life/super-app','statuses_checked':['queued','in_progress','waiting','pending','requested'],
               'matching_Mac_jobs':[],'observed_epoch':time.time()}
        for changes in ({'complete':False},{'matching_Mac_jobs':[{'job':1}]},{'observed_epoch':time.time()-31},{'statuses_checked':['queued']}):
            with self.subTest(changes=changes):
                obj=type('Queue',(),{'queue_probe':lambda self:{**valid,**changes}})()
                with self.assertRaises(RecoveryError):InstallOperations._queue(obj)

    def test_service_absence_requires_specific_native_missing_class_and_old_process_gone(self):
        old={'service':{'pid':1872}}
        for code,stderr,pscode,out,ok in [(113,b'Could not find service com.nycu-life.mac-ci-supervisor',1,b'',True),
                                        (1,b'Operation not permitted',1,b'',False),
                                        (113,b'Could not find service com.nycu-life.mac-ci-supervisor',0,b'old start',False)]:
            with self.subTest(stderr=stderr,pscode=pscode):
                results=iter([subprocess.CompletedProcess([],code,b'',stderr),subprocess.CompletedProcess([],pscode,out,b'')])
                obj=type('Native',(),{'label':'gui/501/com.nycu-life.mac-ci-supervisor','_run':lambda self,args:next(results)})()
                if ok:InstallOperations._absent(obj,old)
                else:
                    with self.assertRaises(RecoveryError):InstallOperations._absent(obj,old)

    def test_unknown_native_mutation_outcome_never_blindly_restores_or_restarts(self):
        obj=type('Native',(),{'native_uncertain':True})()
        with self.assertRaises(RecoveryError):InstallOperations.restore_old(obj,{})

    def test_actual_run_marks_pending_before_dispatch_and_keeps_all_interruption_outcomes(self):
        for verb in ('bootstrap','bootout'):
            for error in (MaintenanceTermination(signal.SIGTERM),MaintenanceTermination(signal.SIGHUP),
                          KeyboardInterrupt(),SystemExit(17),subprocess.TimeoutExpired('fixture',1),OSError('fixture')):
                with self.subTest(verb=verb,error=type(error).__name__):
                    obj=type('Native',(),{'home':Path('/fixture'),'native_uncertain':False})()
                    def interrupted(*args,**kwargs):
                        self.assertTrue(obj.native_uncertain,'pending must precede native dispatch')
                        raise error
                    expected=RecoveryError if isinstance(error,(OSError,subprocess.TimeoutExpired)) else type(error)
                    with mock.patch('supervisor_install.subprocess.run',side_effect=interrupted):
                        with self.assertRaises(expected) as raised:
                            InstallOperations._run(obj,['/bin/launchctl',verb,'fixture'])
                    if not isinstance(error,(OSError,subprocess.TimeoutExpired)):
                        self.assertIs(raised.exception,error,'the original signal/SystemExit must propagate intact')
                    self.assertTrue(obj.native_uncertain)
                    with self.assertRaisesRegex(RecoveryError,'native_mutation_outcome_unknown'):
                        InstallOperations.restore_old(obj,{})

    def test_actual_run_known_completion_resolves_only_its_pending_request(self):
        for verb in ('bootstrap','bootout'):
            for earlier in (False,True):
                for code in (0,1,113):
                    with self.subTest(verb=verb,earlier=earlier,code=code):
                        obj=type('Native',(),{'home':Path('/fixture'),'native_uncertain':earlier})()
                        done=subprocess.CompletedProcess([],code,b'',b'')
                        def completed(*args,**kwargs):
                            self.assertTrue(obj.native_uncertain)
                            return done
                        with mock.patch('supervisor_install.subprocess.run',side_effect=completed):
                            self.assertIs(InstallOperations._run(obj,['/bin/launchctl',verb,'fixture']),done)
                        self.assertEqual(obj.native_uncertain,earlier)

    def test_actual_run_signal_killed_CLI_remains_unknown(self):
        obj=type('Native',(),{'home':Path('/fixture'),'native_uncertain':False})()
        with mock.patch('supervisor_install.subprocess.run',return_value=subprocess.CompletedProcess([],-15,b'',b'')):
            InstallOperations._run(obj,['/bin/launchctl','bootstrap','fixture'])
        self.assertTrue(obj.native_uncertain)

    def test_actual_run_readonly_probe_cannot_clear_earlier_mutation_uncertainty(self):
        obj=type('Native',(),{'home':Path('/fixture'),'native_uncertain':True})()
        with mock.patch('supervisor_install.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')):
            InstallOperations._run(obj,['/bin/launchctl','print','fixture'])
        self.assertTrue(obj.native_uncertain)

    def test_actual_pair_primitive_tracks_replaced_inode_before_fsync_failure(self):
        with tempfile.TemporaryDirectory(prefix='install-pair-fixture-') as directory:
            home=Path(directory);ci=home/'ci';ci.mkdir()
            names=('mac-ci-supervisor.sh','unlock-keychain.py')
            def metadata(path):
                if not path.exists():return None
                s=path.stat();return {'device':s.st_dev,'inode':s.st_ino,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            for path,raw in [('mac-ci-supervisor.sh',b'old'),('new-source',b'new'),('new-helper',b'helper'),('old-source',b'old')]:
                (ci/path).write_bytes(raw)
            obj=type('Files',(),{})();obj.home=home;obj.active_proof={};obj._file=metadata
            obj.verify_boundary=lambda proof:None
            obj.files={name:metadata(ci/name) for name in names}
            obj.staged={'new':{names[0]:(ci/'new-source',metadata(ci/'new-source')),names[1]:(ci/'new-helper',metadata(ci/'new-helper'))},
                        'old':{names[0]:(ci/'old-source',metadata(ci/'old-source'))}}
            obj._sync=lambda path:(_ for _ in ()).throw(OSError(errno.ENOSPC,'fixture_full'))
            with self.assertRaises(OSError):InstallOperations.install_pair(obj,'new')
            self.assertEqual((ci/names[0]).read_bytes(),b'new')
            self.assertEqual(obj.files[names[0]],metadata(ci/names[0]))
            InstallOperations.cleanup_stages(obj)
            self.assertTrue((ci/'old-source').exists(), 'late release failure must still have a rollback file')
            obj._sync=lambda path:None
            InstallOperations.install_pair(obj,'old')
            self.assertEqual((ci/names[0]).read_bytes(),b'old');self.assertFalse((ci/names[1]).exists())

    def test_reused_PID_is_rejected_by_actual_bootstrap_identity_gate(self):
        obj=type('Native',(),{})();obj.home=Path('/fixture');obj.seen_processes={1872}
        obj.verify_boundary=lambda proof:None;obj._authority=lambda kind:None
        obj._run=lambda args:subprocess.CompletedProcess(args,0,b'',b'')
        obj._control=lambda kind:{'service':{'pid':1872,'start':'old'}}
        with mock.patch('supervisor_install.time.monotonic',side_effect=[0,20]):
            with self.assertRaisesRegex(RecoveryError,'replacement_PID_not_fresh'):
                InstallOperations.bootstrap(obj,'new',{'started_epoch':time.time()})


if __name__=='__main__':unittest.main()
