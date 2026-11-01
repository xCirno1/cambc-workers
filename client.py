#!/usr/bin/env python3
"""Owner-side client: pack + seal a bundle, dispatch workers, fetch + decrypt results.

  client.py setup                      # make key, store it as repo secret BOX_KEY
  client.py run A B [--maps GLOB] [--shards N] [--rounds R] [--replays]
A/B are bot names as in tools/arena.py (bot, bot@REF, starter-cpp ...), resolved in --root.
The key lives only in ~/.config/cambc-workers/key and in the GitHub secret.
"""
import argparse, base64, glob, json, os, secrets, shutil, subprocess, sys, tarfile, tempfile, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sealed import seal, unseal

KEYFILE = Path.home() / '.config/cambc-workers/key'


def gh(*args, **kw):
    return subprocess.run(['gh', *args], check=True, capture_output=True, **kw)


def load_key():
    return base64.b64decode(KEYFILE.read_text().strip())


def setup(repo):
    if not KEYFILE.exists():
        KEYFILE.parent.mkdir(parents=True, exist_ok=True)
        KEYFILE.write_text(base64.b64encode(os.urandom(32)).decode() + '\n')
        KEYFILE.chmod(0o600)
    gh('secret', 'set', 'BOX_KEY', '--repo', repo, input=KEYFILE.read_text().strip().encode())
    print('BOX_KEY set on', repo)


def bundle(args, tmp):
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root / 'tools'))
    import arena
    a, b = arena.resolve_bot(args.a), arena.resolve_bot(args.b)
    job = tmp / 'job'
    for name, src in (('a', a), ('b', b)):
        shutil.copytree(Path(src) / 'src', job / 'bots' / name / 'src')
        for f in ('CMakeLists.txt', 'cambc.toml'):
            if (Path(src) / f).exists():
                shutil.copy(Path(src) / f, job / 'bots' / name / f)
    for d in ('sdk', 'cmake'):
        shutil.copytree(root / d, job / d)
    shutil.copy(root / 'cambc.toml', job / 'cambc.toml')
    (job / 'maps').mkdir()
    games = []
    for m in sorted(glob.glob(str(root / args.maps))):
        shutil.copy(m, job / 'maps' / os.path.basename(m))
        for swap in (False, True):
            games.append({'map': 'maps/' + os.path.basename(m), 'swap': swap})
    spec = dict(a='a', b='b', games=games, shards=args.shards, rounds=args.rounds, replays=args.replays)
    (job / 'job.json').write_text(json.dumps(spec))
    arc = tmp / 'input.tar.gz'
    with tarfile.open(arc, 'w:gz') as t:
        t.add(job, arcname='.')
    return arc, len(games)


def run(args):
    key = load_key()
    tag = 'r' + secrets.token_hex(12)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        arc, n = bundle(args, tmp)
        seal(arc, tmp / 'input.box', key)
        gh('release', 'create', tag, str(tmp / 'input.box'), '--repo', args.repo,
           '--title', tag, '--notes', 'sealed')
        gh('workflow', 'run', 'workers.yml', '--repo', args.repo, '-f', f'tag={tag}', '-f', f'shards={args.shards}')
        print(f'{tag}: {n} games over {args.shards} shards; waiting')
        time.sleep(15)
        runs = json.loads(gh('run', 'list', '--repo', args.repo, '--workflow', 'workers.yml',
                             '--json', 'databaseId,displayTitle,status,conclusion', '-L', '30').stdout)
        rid = next(r['databaseId'] for r in runs if r['displayTitle'] == tag)
        while True:
            st = json.loads(gh('run', 'view', str(rid), '--repo', args.repo, '--json', 'status,conclusion').stdout)
            if st['status'] == 'completed':
                break
            time.sleep(30)
        dl = tmp / 'dl'
        gh('run', 'download', str(rid), '--repo', args.repo, '--dir', str(dl))
        out = Path(args.out or f'results/{tag}'); out.mkdir(parents=True, exist_ok=True)
        results = []
        for d in sorted(dl.glob('res-*')):
            rc = json.loads((d / 'receipt.json').read_text())
            if rc['status'] != 'ok':
                print('shard', rc['shard'], 'failed'); continue
            unseal(d / 'results.box', tmp / 'r.json', key)
            results += json.loads((tmp / 'r.json').read_text())
            for nm in ('log', 'replays'):
                if (d / f'{nm}.box').exists():
                    unseal(d / f'{nm}.box', out / f'{nm}-{rc["shard"]}.dec', key)
        (out / 'results.json').write_text(json.dumps(results))
    gh('release', 'delete', tag, '--repo', args.repo, '--yes', '--cleanup-tag')
    summarize(results)


def summarize(results):
    w = l = 0; t = defaultdict(float)
    for r in results:
        d = r['stats']
        if not d:
            continue
        us, them = ('teamB', 'teamA') if r['swap'] else ('teamA', 'teamB')
        o = d['match']['outcome']; w += o == us; l += o == them
        for c in d['champions']:
            t['gems_' + ('us' if c['team'] == us else 'them')] += int(c['metrics']['submittedGems'])
        t['n'] += 1
    n = max(1, t['n'])
    print(f"A vs B: {int(w)}W {int(l)}L of {int(t['n'])}  avg gems {t['gems_us']/n:.0f}-{t['gems_them']/n:.0f}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default='xCirno1/cambc-workers')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('setup')
    r = sub.add_parser('run')
    r.add_argument('a'); r.add_argument('b')
    r.add_argument('--root', default='/mnt/mintfs1/projects/cambc-2026')
    r.add_argument('--maps', default='maps/gen/*.map26a')
    r.add_argument('--shards', type=int, default=4)
    r.add_argument('--rounds', type=int)
    r.add_argument('--replays', action='store_true')
    r.add_argument('--out')
    a = ap.parse_args()
    setup(a.repo) if a.cmd == 'setup' else run(a)
