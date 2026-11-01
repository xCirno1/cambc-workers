"""Run one shard of a sealed bundle. Prints NOTHING about the workload.

Public: this file, opaque tag/shard, ciphertext, a generic "ok/failed" status.
Sealed: bot sources, maps, build output, game logs, results.
The game subprocess gets an allowlisted env (no BOX_KEY, no GH_TOKEN).
"""
import argparse, base64, json, os, re, shutil, subprocess, sys, tarfile
from pathlib import Path
from urllib.request import Request, urlopen

from sealed import seal, unseal

SAFE_ENV = ('PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR')


def child_env(home):
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home), PYTHONUNBUFFERED='1')
    for p in ('.local/bin',):  # uv tool bin dir of the runner user
        env['PATH'] = os.path.expanduser('~/' + p) + ':' + env['PATH']
    return env


def safe_extract(archive, dest):
    dest = dest.resolve()
    with tarfile.open(archive, 'r:gz') as t:
        for m in t.getmembers():
            target = (dest / m.name).resolve()
            if not (m.isfile() or m.isdir()) or not target.is_relative_to(dest):
                raise ValueError('unsafe member')
        t.extractall(dest, filter='data')


def run(cmd, cwd, env, log):
    with open(log, 'ab') as f:
        return subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=f, timeout=5 * 3600).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo'); ap.add_argument('--tag'); ap.add_argument('--shard', type=int)
    ap.add_argument('--out', type=Path)
    a = ap.parse_args()
    assert re.fullmatch(r'[\w.-]+/[\w.-]+', a.repo) and re.fullmatch(r'r[0-9a-f]{24}', a.tag)
    key = base64.b64decode(os.environ['BOX_KEY'], validate=True)
    out = a.out; out.mkdir(parents=True)
    export = out / 'export'; export.mkdir()
    work = out / 'work'; work.mkdir()
    status = 'failed'
    try:
        url = f'https://github.com/{a.repo}/releases/download/{a.tag}/input.box'
        with urlopen(Request(url, headers={'User-Agent': 'cambc-workers'}), timeout=120) as r:
            (out / 'input.box').write_bytes(r.read())
        unseal(out / 'input.box', out / 'input.tar.gz', key)
        safe_extract(out / 'input.tar.gz', work / 'job')
        job = work / 'job'
        spec = json.loads((job / 'job.json').read_text())
        env = child_env(work)
        log = work / 'exec.log'
        results = []
        rc = 0
        for bot in sorted({g[k] for g in spec['games'] for k in ('a', 'b')}):
            rc |= run(['cambc', 'build', f'bots/{bot}', '--release'], job, env, log)
        if rc == 0:
            games = [(i, g) for i, g in enumerate(spec['games']) if i % spec['shards'] == a.shard]
            (job / 'res').mkdir()
            rows = [f'"bots/{g["a"]}" "bots/{g["b"]}" "{g["map"]}" "res/g{i}.repl26a" '
                    f'--statistics "res/g{i}.json" --json' for i, g in games]
            (job / 'matches.txt').write_text('\n'.join(rows) + '\n')
            cmd = ['cambc', 'run-batch', 'matches.txt', '--jobs', str(os.cpu_count() or 2), '--release', '--color', 'never']
            if spec.get('rounds'):
                cmd += ['--rounds', str(spec['rounds'])]
            if spec.get('timeout'):
                cmd += ['--timeout', str(spec['timeout'])]
            run(cmd, job, env, log)
            for i, g in games:
                p = job / 'res' / f'g{i}.json'
                results.append({'idx': i, 'stats': json.loads(p.read_text()) if p.exists() else None})
            if spec.get('replays'):
                shutil.make_archive(str(work / 'replays'), 'gztar', job / 'res')
                seal(work / 'replays.tar.gz', export / 'replays.box', key)
        (work / 'results.json').write_text(json.dumps(results))
        seal(work / 'results.json', export / 'results.box', key)
        if log.exists():
            seal(log, export / 'log.box', key)
        status = 'ok'
    except Exception:
        pass  # exception text could contain workload paths; stay silent
    (export / 'receipt.json').write_text(json.dumps({'tag': a.tag, 'shard': a.shard, 'status': status}))
    sys.exit(0)  # never fail loudly; status lives in the receipt


if __name__ == '__main__':
    main()
