#!/usr/bin/env python3
"""Owner-side client: pack + seal a bundle, dispatch workers, fetch + decrypt results.

  client.py setup                                 # make key, store it as repo secret BOX_KEY
  client.py run A B [--maps GLOB] [--shards N]    # arena-style A vs B, both sides, prints W/L
  client.py chunk MANIFEST --root DIR             # play a `cambc run-batch` manifest (used by bcp's workers lane)
A/B are bot names as in tools/arena.py (bot, bot@REF, starter-cpp ...), resolved in --root.
The key lives only in ~/.config/cambc-workers/key and in the GitHub secret.
"""
import argparse, base64, glob, gzip, json, os, secrets, shlex, shutil, subprocess, sys, tarfile, tempfile, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sealed import seal, unseal

KEYFILE = Path.home() / '.config/cambc-workers/key'
INFRA = 255  # chunk exit code: the workers (not the games) failed; bcp requeues what is missing


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


def no_links(dr, names):
    return [n for n in names if os.path.islink(os.path.join(dr, n))]


def prebuilt(src):
    """The bot's release wasm when it is newer than every source and build file, else None."""
    w = Path(src) / '.cambc' / 'build-release' / 'bot.wasm'
    if not w.exists():
        return None
    srcs = [p for p in (Path(src) / 'src').rglob('*') if p.is_file()]
    srcs += [Path(src) / f for f in ('CMakeLists.txt', 'cambc.toml') if (Path(src) / f).exists()]
    return w if all(p.stat().st_mtime <= w.stat().st_mtime for p in srcs) else None


def pack(root, bots, games, extra, tmp):
    """bots: {name: bot dir}; games: [{a, b, map: path, ...}]. Returns the tarball of the job directory."""
    job = tmp / 'job'
    for name, src in bots.items():
        if prebuilt(src):  # the release wasm bcp already built: the runner skips a 30-70 s compile
            (job / 'bots' / name).mkdir(parents=True)
            shutil.copy(prebuilt(src), job / 'bots' / name / 'bot.wasm')
            continue
        shutil.copytree(Path(src) / 'src', job / 'bots' / name / 'src', ignore=no_links)
        for f in ('CMakeLists.txt', 'cambc.toml'):
            if (Path(src) / f).exists():
                shutil.copy(Path(src) / f, job / 'bots' / name / f)
    for d in ('sdk', 'cmake'):
        shutil.copytree(Path(root) / d, job / d, ignore=no_links)
    shutil.copy(Path(root) / 'cambc.toml', job / 'cambc.toml')
    (job / 'maps').mkdir()
    maps = {}
    for g in games:
        if g['map'] not in maps:
            maps[g['map']] = f'maps/m{len(maps)}.map26a'
            shutil.copy(g['map'], job / maps[g['map']])
    spec = dict(games=[{**g, 'map': maps[g['map']]} for g in games], **extra)
    (job / 'job.json').write_text(json.dumps(spec))
    arc = tmp / 'input.tar.gz'
    with tarfile.open(arc, 'w:gz') as t:
        t.add(job, arcname='.')
    return arc


def dispatch(repo, key, arc, shards, tmp, out):
    """Seal + upload the bundle, run the workflow, wait, decrypt. Returns {game index: stats}; failed shards
    are missing. Raises on transport failure."""
    tag = 'r' + secrets.token_hex(12)
    seal(arc, tmp / 'input.box', key)
    gh('release', 'create', tag, str(tmp / 'input.box'), '--repo', repo, '--title', tag, '--notes', 'sealed')
    try:
        gh('workflow', 'run', 'workers.yml', '--repo', repo, '-f', f'tag={tag}', '-f', f'shards={shards}')
        rid = None
        for _ in range(30):
            time.sleep(2)
            runs = json.loads(gh('run', 'list', '--repo', repo, '--workflow', 'workers.yml',
                                 '--json', 'databaseId,displayTitle', '-L', '40').stdout)
            rid = next((r['databaseId'] for r in runs if r['displayTitle'] == tag), None)
            if rid:
                break
        if not rid:
            raise RuntimeError('workflow run not found')
        while json.loads(gh('run', 'view', str(rid), '--repo', repo, '--json', 'status').stdout)['status'] != 'completed':
            time.sleep(5)
        dl = tmp / 'dl'
        gh('run', 'download', str(rid), '--repo', repo, '--dir', str(dl))
    finally:
        subprocess.run(['gh', 'release', 'delete', tag, '--repo', repo, '--yes', '--cleanup-tag'], capture_output=True)
    out.mkdir(parents=True, exist_ok=True)
    stats = {}
    for d in sorted(dl.glob('res-*')):
        rc = json.loads((d / 'receipt.json').read_text())
        if rc['status'] != 'ok':
            continue
        unseal(d / 'results.box', tmp / 'r.json', key)
        raw = (tmp / 'r.json').read_bytes()
        if raw[:2] == b'\x1f\x8b':  # gzip: newer workers compress the ~370 KB per-game statistics
            raw = gzip.decompress(raw)
        for r in json.loads(raw):
            if r['stats']:
                stats[r['idx']] = r['stats']
        for nm in ('log', 'replays'):
            if (d / f'{nm}.box').exists():
                unseal(d / f'{nm}.box', out / f'{nm}-{rc["shard"]}.dec', key)
    return stats


def chunk(args):
    """Play a run-batch manifest on the workers, writing each game's statistics (and replay) where it names them."""
    rows = [shlex.split(l) for l in Path(args.manifest).read_text().splitlines() if l.strip()]
    names, games, outs = {}, [], []
    for r in rows:
        a, b, m, rep = r[:4]
        st = r[r.index('--statistics') + 1]
        for d in (a, b):
            names.setdefault(d, f'b{len(names)}')
        games.append({'a': names[a], 'b': names[b], 'map': m})
        outs.append((rep, st))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        key = load_key()
        arc = pack(args.root, {v: k for k, v in names.items()}, games,
                   dict(shards=min(args.shards, len(games)), rounds=args.rounds, timeout=args.timeout,
                        replays=args.replays), tmp)
        try:
            stats = dispatch(args.repo, key, arc, min(args.shards, len(games)), tmp, tmp / 'out')
        except Exception as e:  # noqa: BLE001
            print('workers failed:', type(e).__name__, file=sys.stderr)
            sys.exit(INFRA)
        if args.replays:
            for d in (tmp / 'out').glob('replays-*.dec'):
                with tarfile.open(d) as t:
                    t.extractall(tmp / 'rep', filter='data')
        for i, (rep, st) in enumerate(outs):
            if i in stats:
                src = tmp / 'rep' / f'g{i}.repl26a'
                if src.exists():
                    shutil.copy(src, rep)
                Path(st + '.part').write_text(json.dumps(stats[i]))
                os.replace(st + '.part', st)  # last, so bcp never reads a half-written file
    sys.exit(0 if len(stats) == len(outs) else INFRA)


def run(args):
    import importlib
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root / 'tools'))
    arena = importlib.import_module('arena')
    a, b = arena.resolve_bot(args.a), arena.resolve_bot(args.b)
    games = [{'a': x, 'b': y, 'map': m, 'swap': s}
             for m in sorted(glob.glob(str(root / args.maps))) for x, y, s in (('a', 'b', False), ('b', 'a', True))]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        arc = pack(root, {'a': a, 'b': b}, games, dict(shards=args.shards, rounds=args.rounds, replays=args.replays), tmp)
        print(f'{len(games)} games over {args.shards} shards; waiting')
        stats = dispatch(args.repo, load_key(), arc, args.shards, tmp, Path(args.out or f'results/{int(time.time())}'))
    w = l = 0; t = defaultdict(float)
    for i, d in stats.items():
        us, them = ('teamB', 'teamA') if games[i]['swap'] else ('teamA', 'teamB')
        o = d['match']['outcome']; w += o == us; l += o == them
        for c in d['champions']:
            t['gems_' + ('us' if c['team'] == us else 'them')] += int(c['metrics']['submittedGems'])
        t['n'] += 1
    n = max(1, t['n'])
    print(f"A vs B: {w}W {l}L of {int(t['n'])}/{len(games)}  avg gems {t['gems_us']/n:.0f}-{t['gems_them']/n:.0f}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default='xCirno1/cambc-workers')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('setup')
    r = sub.add_parser('run')
    r.add_argument('a'); r.add_argument('b')
    r.add_argument('--maps', default='maps/gen/*.map26a')
    r.add_argument('--out')
    c = sub.add_parser('chunk')
    c.add_argument('manifest')
    c.add_argument('--timeout', type=int)
    for p in (r, c):
        p.add_argument('--root', default='/mnt/mintfs1/projects/cambc-2026')
        p.add_argument('--shards', type=int, default=20)
        p.add_argument('--rounds', type=int)
        p.add_argument('--replays', action='store_true')
    a = ap.parse_args()
    {'setup': lambda: setup(a.repo), 'run': lambda: run(a), 'chunk': lambda: chunk(a)}[a.cmd]()
