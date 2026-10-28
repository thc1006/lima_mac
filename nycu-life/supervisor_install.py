"""Plan-only CLI; coordinated supervisor/helper replacement for a reviewed caller.

This transaction intentionally does not use guarded_phase: restarting launchd
changes PID/start identity. Every replacement process instead has a fresh,
recorded identity bound to one permitted file-pair authority and unchanged plist.
"""
from contextlib import contextmanager
import copy
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import tempfile
import time

if hashlib.sha256(Path(__file__).with_name('maintenance_recovery.py').read_bytes().replace(b'\r\n',b'\n')).hexdigest() != '10b660836aad016657cb65e7d784150ad9087e453065101db7cd7e95f28c60b2':
    raise RuntimeError('reviewed_maintenance_primitives_changed')
from maintenance_recovery import (MacOperations, MaintenanceTermination,
                                  RecoveryError, digest, require,
                                  termination_boundary)

OLD_SUPERVISOR = 'bcb39309baff8017f102dc81ef704d8c26d888b6e63a8a60de9fdbb69db8070a'
INITIAL_SERVICE = {'pid': 99763, 'start': 'Wed Oct 28 10:02:52 2026', 'GOLDEN': 'macos-ci'}
PLIST = '5cf9d1e8be1dc0907919006938d4271d6177e85e6dd89f28fda996046b085e96'
NEW = {'mac-ci-supervisor.sh': 'a31b4ac6e3368ce7b73494eff02218e7328fd12f95d1a39b4069a89ed5734545',
       'unlock-keychain.py': 'a644e6ed903ef3b4b68d9737b259629b0925b4ce5b8e871c7737f749c6d08142'}
PROTECTED = ('macos-ci', 'macos-xcode27-candidate-20261027', 'macos-ci-xcode27-20261027')


def coordinated_install(ops):
    """One transaction, no retry/replay. A recovered failure remains nonzero."""
    result = dict(return_code=1, installed=False, recovered=False,
                  admission_restored=False, evidence_unavailable=[])
    proof = None
    def audit(name, value):
        try:
            ops.record(name, value)
            return True
        except Exception:
            result['evidence_unavailable'].append(name)
            return False
    try:
        with termination_boundary(), ops.lock():
            try:
                proof = ops.prepare()
                ops.record('install-intent.json', proof)
                ops.create_owned_drain(proof)
                ops.verify(proof, 'old', proof['control'])
                ops.wait_idle(proof['control'], timeout=35)
                ops.verify(proof, 'old', proof['control'])
                ops.record('old-bootout-intent.json', {'control': proof['control']})
                ops.bootout(proof['control'])
                ops.install_pair('new')
                ops.record('new-bootstrap-intent.json', {'pair': NEW, 'plist': PLIST})
                control = ops.bootstrap('new', proof)
                ops.wait_idle(control, timeout=35)
                ops.verify(proof, 'new', control)
                # No admission until even the success receipt is durably published.
                ops.record('installed-drained.json', {'control': control, 'pair': NEW})
                ops.cleanup_stages()
                ops.release(proof, 'new', control)
                result.update(return_code=0, installed=True, admission_restored=True,
                              service=control['service'])
            except BaseException as error:
                rc = (128 + error.number if isinstance(error, MaintenanceTermination)
                      else 130 if isinstance(error, KeyboardInterrupt)
                      else error.code if isinstance(error, SystemExit) and type(error.code) is int
                      and 1 <= error.code <= 255 else 1)
                result.update(return_code=rc, failure_type=type(error).__name__)
                if type(error) is RecoveryError:
                    result['failure_reason'] = str(error)
                audit('install-failure.json', result)
                try:
                    if proof is not None and 'marker_identity' in proof:
                        ops.verify_boundary(proof)
                        control = ops.restore_old(proof)
                        ops.wait_idle(control, timeout=35)
                        ops.verify(proof, 'old', control)
                        ops.cleanup_stages()
                        audit('rollback-healthy.json', {'control': control, 'original_return_code': rc})
                        ops.release(proof, 'old', control)
                        result.update(recovered=True, admission_restored=True,
                                      rollback_service=control['service'])
                    elif ops.marker_present():
                        raise RecoveryError('unknown_or_foreign_drain_retained')
                    else:
                        ops.cleanup_stages()
                except BaseException as recovery_error:
                    result.update(recovery_failure_type=type(recovery_error).__name__,
                                  recovery_reason=str(recovery_error) if type(recovery_error) is RecoveryError
                                  else 'private_recovery_failure')
            if not audit('install-outcome.json', result) and result['return_code']==0:
                result.update(return_code=1,post_commit_evidence_failure=True)
    except BaseException as error:
        result.update(return_code=128+error.number if isinstance(error,MaintenanceTermination) else 130 if isinstance(error,KeyboardInterrupt) else 1,
                      preflight_failure_type=type(error).__name__)
    result['evidence_unavailable'].extend(getattr(ops,'unavailable_records',[]))
    return result


class InstallOperations(MacOperations):
    """Native adapter. Constructor and plan CLI perform no native operations.

    The reviewed caller supplies exact protected-storage snapshots and a fresh
    complete queue callback (the existing management controller's queue proof).
    The installer never reads a token/password, calls unlock, or changes plist.
    """
    def __init__(self, operation, *, payload, protected_storage, queue_probe, home=None):
        home = Path.home() if home is None else Path(home)
        stage = home / 'ci/xcode27-guest-prep-20261027'
        super().__init__(operation, Path(operation)/'drain-creation-intent.json', None,
                         lock_path=stage/'toolchain-install.lock', home=home)
        require(home==Path('/Users/thc1006') and self.operation.parent==stage
                and re.fullmatch(r'supervisor-install-[0-9a-f]{32}',self.operation.name),
                'exact_install_operation_scope_required')
        self.payload = dict(payload)
        self.protected_storage = copy.deepcopy(protected_storage)
        self.queue_probe = queue_probe
        self.files = {}
        self.old = {}
        self.staged = {'old': {}, 'new': {}}
        self.initial_control = None
        self.bootstrap_intended = False
        self.seen_processes = set()
        self.unavailable_records = []
        self.active_proof = None
        self.native_uncertain = False

    @contextmanager
    def lock(self):
        require(os.getuid()==os.geteuid()==501,'HOST_UID501_required')
        directory=self.operation.lstat()
        require(stat.S_ISDIR(directory.st_mode) and not self.operation.is_symlink()
                and self.operation.resolve()==self.operation and directory.st_uid==501
                and stat.S_IMODE(directory.st_mode)==0o700,'private_operation_required')
        expected=self._regular(self.shared_lock,True)
        fd=os.open(self.shared_lock,os.O_RDWR|os.O_NOFOLLOW)
        try:
            opened=os.fstat(fd)
            require((opened.st_dev,opened.st_ino)==(expected.st_dev,expected.st_ino),'opened_lock_changed')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RecoveryError('maintenance_lock_busy') from None
            self.lock_identity=(opened.st_dev,opened.st_ino)
            self.operation_identity=(directory.st_dev,directory.st_ino)
            require(self._lock_current(),'shared_lock_replaced')
            yield
        finally:os.close(fd)

    def _lock_current(self):
        value=self._regular(self.shared_lock,True)
        directory=self.operation.lstat()
        return ((value.st_dev,value.st_ino)==self.lock_identity and not self.operation.is_symlink()
                and (directory.st_dev,directory.st_ino)==self.operation_identity)

    def _optional_record(self,name,value):
        try:self.record(name,value)
        except Exception:self.unavailable_records.append(name)

    def _run(self, args, timeout=20):
        mutation = len(args)>1 and args[0]=='/bin/launchctl' and args[1] in ('bootstrap','bootout')
        earlier_uncertainty = self.native_uncertain
        if mutation:
            # A signal can escape subprocess.run after launchd received IPC.
            # Killing/reaping that CLI does not establish the daemon outcome.
            self.native_uncertain = True
        try:
            completed = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                       timeout=timeout, env={'PATH':'/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                                                            'HOME':str(self.home), 'LC_ALL':'C'})
        except (OSError, subprocess.TimeoutExpired):
            raise RecoveryError('bounded_native_command_unavailable') from None
        require(isinstance(completed,subprocess.CompletedProcess) and type(completed.returncode) is int,
                'native_command_result_unknown')
        if mutation and completed.returncode>=0:
            # A normally returned native exit code resolves only this request.
            # A prior uncertainty is sticky; signal-killed CLI results stay unknown.
            self.native_uncertain = earlier_uncertainty
        return completed

    def record(self, name, value):
        require(self._lock_current(),'operation_or_lock_identity_changed')
        super().record(name, value)
        self._sync(self.operation)

    @staticmethod
    def _sync(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _file(self, path):
        if not os.path.lexists(path):
            return None
        before = self._regular(path)
        require(before.st_size <= 256*1024 and not before.st_mode & 0o022, 'source_file_not_protected')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            require((opened.st_dev,opened.st_ino)==(before.st_dev,before.st_ino), 'source_open_identity_changed')
            raw = os.read(fd, 256*1024+1)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        require(len(raw)==before.st_size and (after.st_size,after.st_mtime_ns)==(before.st_size,before.st_mtime_ns), 'source_changed_during_read')
        require((path.lstat().st_dev,path.lstat().st_ino)==(before.st_dev,before.st_ino), 'source_path_changed')
        return {'device':before.st_dev,'inode':before.st_ino,'owner':before.st_uid,
                'mode':stat.S_IMODE(before.st_mode),'links':before.st_nlink,
                'size':before.st_size,'mtime_ns':before.st_mtime_ns,'sha256':digest(raw)}

    def _queue(self):
        value = self.queue_probe()
        require(value.get('complete') is True and value.get('repository')=='nycu-life/super-app'
                and set(value.get('statuses_checked',[]))=={'queued','in_progress','waiting','pending','requested'}
                and value.get('matching_Mac_jobs')==[]
                and type(value.get('observed_epoch')) in (int,float)
                and 0 <= time.time()-value['observed_epoch'] <= 30, 'fresh_complete_idle_Mac_queue_required')
        return value

    def _quiet(self, proof=None):
        plist=self._file(self.home/'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist')
        require(plist is not None and plist['sha256']==PLIST,
                'live_plist_changed')
        rows = self._run([self.lima,'list','--json'])
        require(rows.returncode==0, 'VM_inventory_unavailable')
        values = [json.loads(line) for line in rows.stdout.splitlines() if line.strip()]
        states = {r['name']:r['status'] for r in values}
        require(len(states)==len(values) and all(s=='Stopped' for s in states.values())
                and not any(n.startswith('macos-job-') for n in states), 'all_VMs_stopped_no_job_required')
        require(all(n in states for n in PROTECTED), 'protected_VM_missing')
        require(set(self.protected_storage)==set(PROTECTED), 'exact_three_storage_proofs_required')
        require(all(self.storage(n)==self.protected_storage[n] for n in PROTECTED), 'protected_storage_changed')
        if proof is not None:
            require(states==proof['VM_states'], 'VM_inventory_changed')
        return states

    def _pair(self):
        return {n:self._file(self.home/'ci'/n) for n in NEW}

    def _authority(self, kind):
        current = self._pair()
        require(current==self.files, 'pair_file_identity_changed')
        if kind=='new':
            require(all(current[n] is not None and current[n]['sha256']==h and current[n]['mode']==0o700
                        for n,h in NEW.items()), 'new_pair_authority_changed')
        else:
            require(all((current[n] is None and self.old[n] is None) or
                        (current[n] is not None and self.old[n] is not None and
                         all(current[n][k]==self.old[n][k] for k in ('sha256','mode','owner','size','links')))
                        for n in NEW), 'old_pair_authority_not_restored')

    def _control(self, kind):
        self._authority(kind)
        control = self.control()
        require(control['plist_sha256']==PLIST and control['GOLDEN']=='macos-ci', 'control_authority_changed')
        pid = control['service']['pid']
        row = self._run(['/bin/ps','-p',str(pid),'-o','uid=,pgid=,comm='])
        fields = row.stdout.decode().split()
        require(row.returncode==0 and len(fields)==3 and fields[:2]==['501',str(pid)]
                and fields[2] in ('/bin/bash','bash'), 'service_native_process_identity_changed')
        return control

    def control(self):
        path=self.home/'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist'
        metadata=self._file(path)
        require(metadata is not None and metadata['sha256']==PLIST,'live_plist_changed')
        configured=plistlib.loads(self._read_expected(path,metadata)).get('EnvironmentVariables',{}).get('GOLDEN','macos-ci')
        service=self._service()
        require(configured==service['GOLDEN'],'effective_GOLDEN_changed')
        script=self._file(self.home/'ci/mac-ci-supervisor.sh')
        require(script is not None,'supervisor_missing')
        return {'plist_sha256':metadata['sha256'],'supervisor_sha256':script['sha256'],
                'GOLDEN':configured,'service':service}

    def _initial_control(self):
        control = self._control('old')
        require(control['service'] == INITIAL_SERVICE, 'original_live_identity_changed_requires_review')
        return control

    def prepare(self):
        import sys
        require(sys.platform=='darwin' and os.getuid()==os.geteuid()==501, 'Mac_HOST_UID501_required')
        require(not self.marker_present(), 'foreign_drain_present')
        directory=self.home/'ci';value=directory.lstat()
        require(stat.S_ISDIR(value.st_mode) and not directory.is_symlink() and directory.resolve()==directory
                and value.st_uid==501 and not value.st_mode&0o022, 'ci_directory_not_protected')
        require(set(self.payload)==set(NEW) and all(type(b) is bytes and digest(b)==NEW[n] for n,b in self.payload.items()), 'exact_reviewed_pair_required')
        self.files=self._pair();self.old=dict(self.files)
        require(self.old['mac-ci-supervisor.sh'] is not None and self.old['mac-ci-supervisor.sh']['sha256']==OLD_SUPERVISOR,
                'exact_old_supervisor_required')
        control=self._initial_control();self.initial_control=control
        self.seen_processes.add(control['service']['pid'])
        proof={'control':control,'VM_states':self._quiet(),'storage':self.protected_storage,
               'queue':self._queue(),'old_pair':self.old,'started_epoch':time.time()}
        for name in NEW:
            path=self.home/'ci'/name
            if self.old[name] is not None:
                raw=self._read_expected(path,self.old[name])
                self._backup(name,raw)
                self.staged['old'][name]=self._stage(name,raw,self.old[name]['mode'],directory=self.operation)
            self.staged['new'][name]=self._stage(name,self.payload[name],0o700)
        proof['staged_pair']={kind:{n:{'path':str(p),'identity':meta} for n,(p,meta) in files.items()}
                              for kind,files in self.staged.items()}
        proof['rollback_authorization']='Restore only captured old pair/absent helper, bootstrap unchanged plist with fresh source-bound PID, release exact owned drain after full healthy proof.'
        self._sync(self.home/'ci')
        self.active_proof=proof
        return proof

    def _backup(self,name,raw):
        path=self.operation/('before-'+name)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        require(digest(path.read_bytes())==digest(raw), 'private_backup_not_verified')

    def _read_expected(self,path,expected):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            value=os.fstat(fd)
            require((value.st_dev,value.st_ino,value.st_uid,value.st_nlink,stat.S_IMODE(value.st_mode),value.st_size)
                    ==tuple(expected[k] for k in ('device','inode','owner','links','mode','size')),'backup_open_identity_changed')
            raw=os.read(fd,256*1024+1)
        finally:os.close(fd)
        require(digest(raw)==expected['sha256'] and self._file(path)==expected,'backup_source_changed')
        return raw

    def _stage(self,name,raw,mode,*,directory=None):
        directory=self.home/'ci' if directory is None else directory
        require(directory.stat().st_dev==(self.home/'ci').stat().st_dev,'pair_staging_filesystem_changed')
        fd,temp=tempfile.mkstemp(prefix='.'+name+'.'+self.operation.name+'.',dir=directory)
        path=Path(temp)
        identity=os.fstat(fd)
        try:
            with os.fdopen(fd,'wb') as f:
                os.fchmod(f.fileno(),mode);f.write(raw);f.flush();os.fsync(f.fileno())
        except BaseException:
            current=path.lstat()
            if stat.S_ISREG(current.st_mode) and current.st_nlink==1 and current.st_uid==501 and (current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino):
                path.unlink()
            raise
        return path,self._file(path)

    def verify_boundary(self,proof):
        require(self._lock_current(),'shared_lock_replaced')
        self._quiet(proof)
        require(self.marker_identity()==proof['marker_identity'] and self.creation_record_hash()==proof['creation_record_sha256'],
                'owned_drain_or_creation_identity_changed')

    def verify(self,proof,kind,control):
        self.verify_boundary(proof)
        require(self._control(kind)==control, 'expected_service_process_changed')
        self._queue()

    def _absent(self,last_control):
        value=self._run(['/bin/launchctl','print',self.label])
        require(value.returncode!=0 and b'Could not find service' in value.stderr
                and b'com.nycu-life.mac-ci-supervisor' in value.stderr, 'service_absence_not_proven')
        old=self._run(['/bin/ps','-p',str(last_control['service']['pid']),'-o','lstart='])
        require(old.returncode==1 and not old.stdout.strip(), 'prior_process_not_gone')

    def bootout(self,control):
        require(self.control()==control, 'bootout_target_changed')
        value=self._run(['/bin/launchctl','bootout',self.label])
        self._absent(control)
        require(value.returncode==0, 'native_bootout_failed')

    def install_pair(self,kind):
        for name in NEW:
            self.verify_boundary(self.active_proof)
            dest=self.home/'ci'/name
            require(self._file(dest)==self.files[name], 'target_file_not_owned')
            staged=self.staged[kind].get(name)
            if staged is None:
                if self.files[name] is not None:
                    dest.unlink();self.files[name]=None
            else:
                path,meta=staged;require(self._file(path)==meta,'staged_pair_identity_changed')
                os.replace(path,dest)
                # Ownership is tracked before any subsequent fsync/audit can fail.
                self.files[name]=meta
            self._sync(self.home/'ci')

    def bootstrap(self,kind,proof):
        self.verify_boundary(proof)
        self._authority(kind)
        self.bootstrap_intended=True
        value=self._run(['/bin/launchctl','bootstrap','gui/501',str(self.home/'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist')])
        deadline=time.monotonic()+15
        while True:
            try:
                control=self._control(kind)
                require(control['service']['pid'] not in self.seen_processes, 'replacement_PID_not_fresh')
                started=time.mktime(time.strptime(control['service']['start'],'%a %b %d %H:%M:%S %Y'))
                require(proof['started_epoch']-1<=started<=time.time()+1, 'replacement_start_not_fresh')
                self.seen_processes.add(control['service']['pid'])
                if kind=='old':self._optional_record('old-service-observed.json',{'control':control,'pair_authority':kind})
                else:self.record('new-service-observed.json',{'control':control,'pair_authority':kind})
                require(value.returncode==0, 'native_bootstrap_failed')
                return control
            except RecoveryError:
                if time.monotonic()>=deadline:raise
                time.sleep(.25)

    def restore_old(self,proof):
        require(not self.native_uncertain,'native_mutation_outcome_unknown_requires_review')
        self.verify_boundary(proof)
        # Files are ours even when a replacement failed halfway. A foreign inode
        # blocks restoration; never overwrite a concurrent operator's changes.
        require(self._pair()==self.files,'rollback_pair_identity_changed')
        try:
            current=self.control()
        except RecoveryError:
            current=None
        if current==proof['control'] and self.files==self.old:
            return self._control('old')
        if current is not None:
            require(self.bootstrap_intended,'unknown_service_not_owned')
            kind='new' if self.files['mac-ci-supervisor.sh']['sha256']==NEW['mac-ci-supervisor.sh'] else 'old'
            current=self._control(kind)
            require(current['service']['pid']!=proof['control']['service']['pid'], 'unknown_original_process_not_stopped')
            started=time.mktime(time.strptime(current['service']['start'],'%a %b %d %H:%M:%S %Y'))
            require(proof['started_epoch']-1<=started<=time.time()+1,'rollback_process_start_not_owned')
            self._optional_record('replacement-bootout-intent.json',{'control':current,'pair_authority':kind})
            self.bootout(current)
        else:
            self._absent(proof['control'])
        self.install_pair('old')
        # Rollback authorization and old files were durable before mutation;
        # later ENOSPC cannot make an optional audit record strand that rollback.
        self._optional_record('old-bootstrap-intent.json',{'pair':self.old,'plist':PLIST})
        return self.bootstrap('old',proof)

    def cleanup_stages(self):
        # Keep old staging in private operation evidence: release can still fail
        # after this cleanup. Its complete rollback pair must remain available.
        for path,meta in self.staged['new'].values():
            if os.path.lexists(path):
                require(self._file(path)==meta,'staged_cleanup_identity_changed')
                path.unlink()

    def release(self,proof,kind,control):
        self.verify(proof,kind,control)
        record={'authority':kind,'control':control,'marker_identity':proof['marker_identity']}
        if kind=='old':self._optional_record('old-drain-release-intent.json',record)
        else:self.record('new-drain-release-intent.json',record)
        self.verify(proof,kind,control)
        require(self.marker_identity()==proof['marker_identity'],'marker_changed_before_release')
        self.remove_marker()
        require(not self.marker_present(),'owned_marker_release_not_verified')
        # Normal admission may now call the supervisor's existing private PTY
        # unlock. No VM quiet assertion or rollback is safe after this boundary.


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps({'mode':'plan_only','old_supervisor':OLD_SUPERVISOR,
                      'unchanged_plist':PLIST,'new_pair':NEW,
                      'native_operations_performed':False,
                      'apply_entrypoint':'Requires separately reviewed caller and fresh complete queue callback; no CLI apply.'},indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
