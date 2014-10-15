#!/usr/bin/env python

"""
(qb conf) ->
    print qb conf

(qb qubits [TARGETS]) ->
    print all the qubits for TARGETS

(qb make [TARGETS]) ->
    start making TARGETS from Qfile

(qb pack [TARGETS]) ->
    create QPACK for TARGETS

(qb seed [TARGETS]) ->
    start making TARGETS from QUBITS_FILE
    continue until QUBITS_FILE complete

(qb spawn [JOBID] [QPACK]) ->
    ssh each NODE:
     cd [JOBID] job directory
     spawn `qb seed -j [JOBID] -p PROFILE DIVVY(TARGETS from QPACK)`
    splits TARGETS across both processes & nodes

(qb share [QPACK]) ->
    copies QPACK to all nodes, unpacks
    returns JOBID

(qb kill [JOBISH] [SIGNAL]) ->
    ssh each NODE:
     pkill -SIGNAL "qb seed [-j JOBISH]"

(qb run [TARGETS]) ->
    qb pack TARGETS
    qb spawn `qb share`
"""

import os
import re
import socket
import sys
import time
import uuid

from fnmatch import fnmatch
from itertools import chain, groupby
from shutil import copytree, rmtree
from subprocess import Popen, PIPE
from urllib import quote_plus
from warnings import warn

def log(o):
    if conf.get('verbose'):
        sys.stderr.write(str(o) + '\n')

def sh(cmd, *args, **opts):
    log("Calling %s with %s and %s" % (cmd, args, opts))
    return Popen(cmd, *args, **opts)

def cat(data, proc):
    proc.stdin.write(data)
    proc.stdin.close()
    proc.wait()

def pscp(src, dsts, scp='scp', P=16):
    xargs = ('xargs', '-t') if conf.get('verbose') else ('xargs',)
    scp = scp + ' -v' if conf.get('verbose') else scp + ' -q'
    proc = sh(xargs + ('-L', '1', '-P', str(P), 'bash', '-c', "%s $1 $2" % scp, '-'), stdin=PIPE)
    cat('\n'.join('%s\t%s' % (src, dst) for dst in dsts), proc)

def pssh(orders, ssh='ssh', P=16):
    xargs = ('xargs', '-t') if conf.get('verbose') else ('xargs',)
    fmt = r"\e[35m@: %s \e[0m\n%s\n\e[36m?: %s\e[0m\n"
    enc = r"printf \"%s\" $1 \"\`(${*:2}) 2>&1\`\" \$?" % fmt
    proc = sh(xargs + ('-L', '1', '-P', str(P), 'bash', '-c', "%s $1 \"%s\"" % (ssh, enc), '-'), stdin=PIPE)
    cat('\n'.join('%s\t%s' % o for o in orders), proc)

def dotfile(path):
    return os.path.basename(path).startswith('.')

class Job(object):
    def __init__(self, jobspace, id=None):
        self.jobspace = jobspace
        self.id = id

    def __enter__(self):
        if not self.id:
            self.id = uuid.uuid1().hex
        self.jobspace.subspace(self.id)
        return self

    def __exit__(self, *args):
        pass # cleanup

    def status(self, qubit, qbdict):
        i, o = self.punch_count(qubit)
        if o:
            return 'up-to-date', (i, o)
        if all(self.status((d, qbdict[d]), qbdict)[0] == 'up-to-date' for d in qbdeps(qubit)):
            return 'ready', (i, o)
        return 'waiting', (i, o)

    def sync(self):
        return self.jobspace.sync(self.id)

    def punch_clock(self, qubit, inout):
        return self.jobspace.punch_clock(self.id, qubit, inout)

    def punch_count(self, qubit):
        return self.jobspace.punch_count(self.id, qubit)

class JobSpace(object):
    def __new__(cls, url, *args):
        if cls is JobSpace:
            if url.startswith('s3://'):
                return JobSpace.__new__(S3JobSpace, url, *args)
            return JobSpace.__new__(FileJobSpace, url, *args)
        return super(JobSpace, cls).__new__(cls, url, *args)

    def __init__(self, url, worker, qspace):
        self.url = url
        self.worker = worker
        self.qspace = qspace

    def __repr__(self):
        return '%s(%r, %r)' % (type(self).__name__, self.url, self.worker)

class FileJobSpace(JobSpace):
    def __init__(self, url, *args):
        JobSpace.__init__(self, url, *args)
        self.path = self.url

    def punch_clock(self, sub, qubit, inout):
        with open(os.path.join(self.path, sub, quote_plus(self.worker)), 'a') as clock:
            clock.write('%s\t%s\t%d\n' % (time.time(), qbtarget(qubit), inout))

    def punch_count(self, sub, qubit):
        i, o = 0, 0
        target = qbtarget(qubit)
        subdir = os.path.join(self.path, sub)
        for worker in os.listdir(subdir):
            for line in open(os.path.join(subdir, worker)):
                t, target_, inout = line.strip().split('\t')
                if target == target_:
                    if inout == '1':
                        i += 1
                    else:
                        o += 1
        return i, o

    def subspace(self, id):
        sh(('mkdir', '-p', os.path.join(self.path, id))).wait()

    def sync(self, id):
        pass

class S3JobSpace(FileJobSpace):
    def __init__(self, url, *args):
        FileJobSpace.__init__(self, url, *args)
        self.path = os.path.join(self.qspace, quote_plus(self.url))

    def sync(self, id):
        sh(('aws', 's3', 'sync', self.path, self.url)).wait()

class Config(dict):
    def __call__(self, key):
        def store(val):
            self[key] = val
            return val
        return store

    def expand(self, key, default=None):
        maybe = self.get(key, default)
        return maybe() if callable(maybe) else maybe

    def jobdir(self, id):
        return os.path.join(self['jobroot'], self.get('jobprefix', '') + id)

    def jobspace(self, url=None):
        if url is None:
            return self.get('jobspace') or self.jobspace(self['qspace'])
        return self('jobspace')(JobSpace(url, self['worker'], self['qspace']))

conf = Config({
    'parent': None,
    'profile': None,
    'qpack': '.qpack',
    'qubits': '.qubits',
    'qspace': '.qspace',
    'interval': 2,
    'stalled': 100,
    'jobroot': '/mnt',
    'jobprefix': 'qjob-',
    'nodes': [('localhost', 2)],
    'worker': '%s:%s' % (socket.gethostname(), os.getpid()),
    'spawnlog': 'spawn.log',
})
rules = []

def rule(regexp, deps=None, rules=rules):
    pattern = re.compile(regexp)
    def recipe(do):
        rules.append((pattern, deps, do))
        return do
    return recipe

def qbread(lines, rules=rules):
    for line in lines:
        yield qbparse(line, rules)

def qbparse(line, rules=rules):
    _name, target, deps = line.strip('\n').split('\t')
    _deps, do = match(target, rules=rules)
    return target, (deps.split(' ') if deps else [], do)

def qbformat((target, (deps, do))):
    return "%s\t%s\t%s\n" % (do.__name__, target, ' '.join(deps))

def qbdumps(qubits):
    return ''.join(qbformat(qubit) for qubit in qubits)

def qbtarget((target, (deps, do))):
    return target

def qbdeps((target, (deps, do))):
    return deps

def qbname((target, (deps, do))):
    return do.__name__

def qbcall((target, (deps, do))):
    return do(target, *deps)

def expand(deps, m=None):
    if callable(deps):
        return deps(*(m.groups() if m else ()))
    if isinstance(deps, basestring):
        return deps,
    return deps or ()

def match(target, rules=rules):
    for pattern, deps, do in rules:
        m = pattern.match(target)
        if m:
            return expand(deps, m), do
    raise ValueError("Don't know how to make '%s'" % target)

def qubits_(target, qubits=None, ancestors=(), rules=rules):
    qubits = qubits or {}
    priors = ancestors + (target,)
    deps, do = match(target, rules=rules)
    qubits[target] = deps, do
    for dep in deps:
        if dep in priors:
            warn("Dropping circular dependency: %s, %s" % (priors, dep), Warning)
            del qubits[target]
        else:
            qubits = qubits_(dep, qubits, priors, rules)
    return qubits

def qubits(targets=(), rules=rules):
    return sum((qubits_(t, rules=rules).items() for t in targets or ('default',)), [])

def loop(qubits, job, conf=conf):
    idle = 0
    stalled = conf['stalled']
    interval = conf['interval']
    qubits = list(qubits)
    qbdict = dict(qubits)
    targets = set(t for t, _ in qubits)
    while targets:
        busy = False
        if idle:
            time.sleep(interval)
        job.sync()
        for qubit in qubits:
            target = qubit[0]
            if target in targets:
                stat, (i, o) = job.status(qubit, qbdict)
                log("%12s (%s, %s): %s" % (stat, i, o, target))
                if stat == 'up-to-date':
                    targets.remove(target)
                elif stat == 'waiting':
                    pass
                elif i == 0 or idle > stalled:
                    job.punch_clock(qubit, True)
                    qbcall(qubit)
                    job.punch_clock(qubit, False)
                    busy = True
        idle = 0 if busy else idle + 1

def make(targets=(), conf=conf, rules=rules):
    with Job(conf.jobspace(), id=conf['parent']) as job:
        loop(qubits(targets, rules), job, conf=conf)
    return job.id

def pack(targets=(), conf=conf):
    qp = conf['qpack']
    qs = conf['qubits']
    def ignored(path, globs=[l.strip() for l in conf.expand('ignore', [])]):
        return any(fnmatch(path, i) for i in globs + ['*.pyc', '.q*', 'Qfilec'])
    def ignore(dir, names):
        return [n for n in names if ignored(n) or dotfile(n) or n == qp]
    if os.path.exists(qp):
        rmtree(qp)
    copytree('.', qp, symlinks=True, ignore=ignore)
    with open(os.path.join(qp, qs), 'w') as file:
        file.write(qbdumps(qubits(targets)))
    return qp

def seed(targets=(), conf=conf, rules=rules):
    qd = dict(qbread(open(conf['qubits']), rules=rules))
    ts = chain(targets, (t for t in qd if t not in targets))
    with Job(conf.jobspace(), id=conf['parent']) as job:
        loop(((t, qd[t]) for t in ts), job)
    return job.id

def spawn(jobid, qpack=None, conf=conf, rules=rules):
    qp = qpack or conf['qpack']
    qs = conf['qubits']
    sl = conf['spawnlog']
    ps = sum(([(addr, [])] * nmax for addr, nmax in conf.expand('nodes')), [])
    qs = qbread(open(os.path.join(qp, qs)), rules=rules)
    for n, qubit in enumerate(q for q in qs if not qbdeps(q)):
        ps[n % len(ps)][1].append(qbtarget(qubit))
    with Job(conf.jobspace(), id=jobid) as job:
        flags = '-j %s' % job.id
        if conf.get('profile'):
            flags += ' -p %s' % conf['profile']
        if conf.get('verbose'):
            flags += ' -v'
        def plant(targets):
            return '(nohup ./qb.py seed %s %s >> %s 2>&1 &)' % (flags, ' '.join(targets), sl)
        pssh(((addr, 'cd %s; %s; echo ok' %
               (conf.jobdir(job.id), '; '.join(plant(ts) for _addr, ts in group if ts)))
              for addr, group in groupby(ps, lambda (k, v): k)))
    return jobid

def share(qpack=None, conf=conf):
    qp = qpack or conf['qpack']
    with Job(conf.jobspace(), id=conf['parent']) as job:
        pscp(qp + '/',
             ('%s:%s' % (addr, conf.jobdir(job.id))
              for addr, nmax in conf.expand('nodes')),
             scp='rsync -az')
    return job.id

def kill(jobish=None, signal='KILL', conf=conf):
    flags = ('-j %s' % jobish) if jobish else ''
    pssh((addr, r'pkill -%s -f \"qb.py seed %s\"' % (signal or 'KILL',  flags))
         for addr, _nmax in conf.expand('nodes'))

def run(targets=()):
    qpack = pack(targets)
    return spawn(share(qpack), qpack)

def load(filename='Qfile'):
    import imp
    return imp.load_source('Qfile', filename)

def cli_conf():
    for k in sorted(conf):
        print('%12s:\t%r' % (k, conf.expand(k)))

def cli_qubits(*targets):
    for qubit in qubits(targets):
        sys.stdout.write(qbformat(qubit))

def cli_make(*targets):
    make(targets)

def cli_pack(*targets):
    print(pack(targets))

def cli_seed(*targets):
    print(seed(targets))

def cli_spawn(jobid, qpack=None):
    print(spawn(jobid, qpack=None))

def cli_share(qpack=None):
    print(share(qpack))

def cli_kill(jobish=None, signal=None):
    kill(jobish, signal)

def cli_run(*targets):
    print(run(targets))

def cli_help(*args):
    print(__doc__.strip())

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--Qfile',
                        help="the path of the Qfile",
                        default='Qfile')
    parser.add_argument('-j', '--parent',
                        help="the parent job")
    parser.add_argument('-p', '--profile',
                        help="the profile of the config")
    parser.add_argument('-v', '--verbose',
                        action='store_true',
                        help="enable verbose output")
    opts, args = parser.parse_known_args(sys.argv[1:])
    cmd, args = args[0] if args else 'help', args[1:]
    parent = conf['parent'] = opts.parent
    profile = conf['profile'] = opts.profile or ('dist' if cmd == 'run' else None)
    verbose = conf['verbose'] = opts.verbose
    Qfile = load(opts.Qfile)
    eval('cli_' + cmd)(*args)

if __name__ == '__main__':
    sys.modules['qb'] = sys.modules['__main__'] # NB: horrible import hack
    main()
