#!/usr/bin/env python3
"""Rebuild the missing `moov` atom of a broken dashcam MP4 (Novatek-style layout).

The camera writes a small `nidx` index box (offset/size/keyframe flag of every
video and audio sample) after each second of footage, and only writes `moov` at
the very end. When recording is cut off, `moov` is missing but the `nidx` boxes
are still in the file. This script:

  * reads every `nidx` box and rebuilds the sample tables from them,
  * detects blocks of data that are missing from the file (the stored offsets
    stop matching the data), re-aligns the samples behind each gap and drops
    the frames that were lost (plus the frames that depend on them),
  * recovers the last second(s) after the final `nidx` box by walking the data,
  * copies codec setup, timescales and frame durations from a working reference
    clip recorded by the same camera with the same settings,
  * writes a new MP4 (the input is never modified).

Usage:
    python rebuild_dashcam_moov.py BROKEN.MP4 --ref WORKING.MP4 [-o OUT.MP4]
"""
import argparse
import collections
import datetime
import mmap
import re
import shutil
import struct
import sys
from pathlib import Path

EPOCH_1904 = 2082844800
CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}


def die(msg):
    sys.exit('error: ' + msg)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def box(t, payload):
    return struct.pack('>I', len(payload) + 8) + t + payload


def children(d, start, end):
    pos = start
    while pos + 8 <= end:
        n, t = struct.unpack_from('>I4s', d, pos)
        if n < 8 or pos + n > end:
            break
        yield t, pos, n
        pos += n


def find_top_level(d, wanted):
    pos, L = 0, len(d)
    while pos + 8 <= L:
        n, t = struct.unpack_from('>I4s', d, pos)
        if t == wanted:
            return pos, n
        if n == 1:
            n = struct.unpack_from('>Q', d, pos + 8)[0]
        if n < 8:
            return None
        pos += n
    return None


class Source:
    def __init__(self, b):
        self.b = b
        self.L = len(b)

    def nal_chain_ok(self, o, s):
        """True if [o, o+s) is exactly a chain of 4-byte-length-prefixed NAL units."""
        b, L = self.b, self.L
        if o < 0 or s < 5 or o + s > L:
            return False
        q, end = o, o + s
        while q < end:
            if q + 5 > end:
                return False
            n = struct.unpack_from('>I', b, q)[0]
            if n == 0 or q + 4 + n > end or b[q + 4] & 0x80:
                return False
            q += 4 + n
        return True

    def plausible_video(self, o, min_size=2000, bounded=True):
        if o < 0 or o + 8 > self.L:
            return False
        n = struct.unpack_from('>I', self.b, o)[0]
        t = self.b[o + 4]
        return (min_size < n < 4_000_000 and not t & 0x80 and (t & 0x1f) in (1, 5)
                and t & 0x60 and (not bounded or o + 4 + n <= self.L))

    def skip_free(self, pos):
        while pos + 8 <= self.L and self.b[pos + 4:pos + 8] == b'free':
            sz = struct.unpack_from('>I', self.b, pos)[0]
            if sz < 8 or sz > 0x10000:
                break
            pos += sz
        return pos


# --------------------------------------------------------------------------
# nidx index boxes
# --------------------------------------------------------------------------
def parse_indexes(b):
    L = len(b)
    found = []
    for m in re.finditer(b'nidx', b):
        p = m.start() - 4
        if p < 0:
            continue
        bs = struct.unpack_from('>I', b, p)[0]
        if not 64 <= bs <= (1 << 20) or p + bs > L:
            continue
        w = struct.unpack_from('<%dI' % ((bs - 28) // 4), b, p + 28)
        if len(w) < 11:
            continue
        nv = w[9]
        k = 10 + 4 * nv
        if k + 1 > len(w):
            continue
        na = w[k]
        if k + 1 + 4 * na > len(w):
            continue
        vr = [(w[10 + 4 * i] | w[11 + 4 * i] << 32, w[12 + 4 * i], w[13 + 4 * i] & 1) for i in range(nv)]
        ar = [(w[k + 1 + 4 * i] | w[k + 2 + 4 * i] << 32, w[k + 3 + 4 * i]) for i in range(na)]
        found.append(((w[1], w[2], w[4], w[5]), p, vr, ar))
    if not found:
        die('no nidx index boxes found - this file does not use the expected camera layout')
    modal = collections.Counter(f[0] for f in found).most_common(1)[0][0]
    found = [f for f in found if f[0] == modal]
    video, audio = [], []
    for _, _, vr, ar in found:
        video += vr
        audio += ar
    video.sort(key=lambda x: x[0])
    audio.sort(key=lambda x: x[0])
    return modal, found, video, audio


# --------------------------------------------------------------------------
# gap detection
# --------------------------------------------------------------------------
def locate_video(src, vs, max_lost):
    """Return per-sample actual offsets (None = lost) and the list of gaps.

    A gap is (zone_start_orig, zone_end_orig, total_shift): samples whose index
    offset lies behind the gap are stored `total_shift` bytes earlier.
    """
    b = src.b
    n = len(vs)
    cur = 0
    actual = []
    gaps = []
    dead_from = None
    i = 0
    while i < n:
        o, s, _ = vs[i]
        if src.nal_chain_ok(o - cur, s):
            actual.append(o - cur)
            i += 1
            continue
        lo = 0
        straddler = False
        if actual and actual[-1] is not None:
            lo = actual[-1] + 1
            straddler = True
        found = None
        for j in range(i, min(n, i + max_lost)):
            oj, sj, _ = vs[j]
            hi = min(oj - cur - 1, src.L - sj)
            if hi < lo:
                continue
            pat = struct.pack('>I', sj - 4)
            pos = lo
            while True:
                pos = b.find(pat, pos, hi + 4)
                if pos < 0:
                    break
                d = oj - pos
                nxt_ok = j + 1 >= n or src.nal_chain_ok(vs[j + 1][0] - d, vs[j + 1][1])
                if src.nal_chain_ok(pos, sj) and nxt_ok:
                    found = (j, d)
                    break
                pos += 1
            if found:
                break
        if not found:
            dead_from = o
            actual.extend([None] * (n - i))
            break
        j, d = found
        if straddler:
            # the frame in which the data stops is corrupt even if its header is fine
            actual[-1] = None
            zs = vs[i - 1][0]
        else:
            zs = o
        actual.extend([None] * (j - i))
        gaps.append((zs, vs[j][0], d))
        cur = d
        i = j
    return actual, gaps, dead_from


def drop_dependents(vs, actual):
    broken = False
    for k, (o, s, sync) in enumerate(vs):
        if actual[k] is None:
            broken = True
        elif sync:
            broken = False
        elif broken:
            actual[k] = None


def learn_audio_signature(src, audio, limit_orig):
    stats = [collections.Counter() for _ in range(8)]
    count = 0
    for o, s in audio:
        if o >= limit_orig:
            break
        if o + s > src.L or s < 8:
            continue
        count += 1
        for k in range(8):
            stats[k][src.b[o + k]] += 1
    sig = []
    for k in range(8):
        if stats[k] and count:
            v, c = stats[k].most_common(1)[0]
            if c / count >= 0.98:
                sig.append((k, v))
    return sig


def locate_audio(src, audio, gaps, dead_from, sig):
    out = []
    for o, s in audio:
        shift, zone = 0, False
        for zs, ze, d in gaps:
            if o >= ze:
                shift = d
            elif o >= zs:
                shift, zone = d, True
                break
            else:
                break
        a = o - shift
        if a < 0 or a + s > src.L or (dead_from is not None and o >= dead_from):
            out.append(None)
        elif zone and not (sig and all(src.b[a + k] == v for k, v in sig)):
            out.append(None)
        else:
            out.append(a)
    return out


def walk_tail(src, pos):
    """Walk video/audio samples that follow the last nidx box."""
    vids, auds = [], []
    while src.plausible_video(pos):
        n = struct.unpack_from('>I', src.b, pos)[0]
        s = n + 4
        vids.append((pos, s, (src.b[pos + 4] & 0x1f) == 5))
        pos = src.skip_free((pos + s + 3) & ~3)
        found = None
        for k in (1, 2, 3):
            for total in sorted(range(k * 376, k * 392 + 1), key=lambda t: abs(t - k * 384)):
                cand = src.skip_free(pos + total)
                if src.plausible_video(cand, bounded=False):
                    found = (k, total, cand)
                    break
            if found:
                break
        if not found:
            break
        k, total, cand = found
        a = pos
        for j in range(k):
            sz = 384 if j < k - 1 else total - 384 * (k - 1)
            auds.append((a, sz))
            a += sz
        pos = cand
    return vids, auds


# --------------------------------------------------------------------------
# reference clip
# --------------------------------------------------------------------------
def read_reference(ref):
    mp = find_top_level(ref, b'moov')
    if not mp:
        die('reference file has no moov atom - it must be a working recording')
    info = {'moov': mp, 'tracks': {}}
    mvhd = next(c for c in children(ref, mp[0] + 8, mp[0] + mp[1]) if c[0] == b'mvhd')
    info['movie_ts'] = struct.unpack_from('>I', ref, mvhd[1] + 8 + 12)[0]
    for t, p, n in children(ref, mp[0] + 8, mp[0] + mp[1]):
        if t != b'trak':
            continue
        kind = ts = durs = None
        for t2, p2, n2 in children(ref, p + 8, p + n):
            if t2 != b'mdia':
                continue
            for t3, p3, n3 in children(ref, p2 + 8, p2 + n2):
                if t3 == b'mdhd':
                    ts = struct.unpack_from('>I', ref, p3 + 8 + 12)[0]
                elif t3 == b'hdlr':
                    kind = ref[p3 + 16:p3 + 20]
                elif t3 == b'minf':
                    for t4, p4, n4 in children(ref, p3 + 8, p3 + n3):
                        if t4 != b'stbl':
                            continue
                        for t5, p5, n5 in children(ref, p4 + 8, p4 + n4):
                            if t5 == b'stts':
                                cnt = struct.unpack_from('>I', ref, p5 + 12)[0]
                                durs = collections.Counter()
                                for e in range(cnt):
                                    c, d = struct.unpack_from('>II', ref, p5 + 16 + 8 * e)
                                    durs[d] += c
        if kind in (b'vide', b'soun') and ts and durs:
            info['tracks'][kind] = {'ts': ts, 'dur': durs.most_common(1)[0][0]}
    if b'vide' not in info['tracks'] or b'soun' not in info['tracks']:
        die('reference file needs one video and one audio track')
    return info


# --------------------------------------------------------------------------
# moov writer
# --------------------------------------------------------------------------
def build_track(samples, unit):
    """samples: list of (actual|None, size, sync). Lost samples extend the previous one."""
    kept, pending = [], 0
    for o, s, sync in samples:
        if o is not None:
            kept.append([o, s, sync, unit + pending])
            pending = 0
        elif kept:
            kept[-1][3] += unit
        else:
            pending += unit
    return kept


def make_moov(ref, info, tracks, creation):
    ctime = int(creation.timestamp()) + EPOCH_1904
    mts = info['movie_ts']
    total = {k: sum(x[3] for x in tr) for k, tr in tracks.items()}
    mdur = {k: total[k] * mts // info['tracks'][k]['ts'] for k in tracks}
    movie_dur = max(mdur.values())

    def trak_kind(p, n):
        for t2, p2, n2 in children(ref, p + 8, p + n):
            if t2 == b'mdia':
                for t3, p3, _ in children(ref, p2 + 8, p2 + n2):
                    if t3 == b'hdlr':
                        return ref[p3 + 16:p3 + 20]

    def rle(durs):
        out = []
        for d in durs:
            if out and out[-1][1] == d:
                out[-1][0] += 1
            else:
                out.append([1, d])
        return out

    def rebuild(pos, size, ctx):
        t = ref[pos + 4:pos + 8]
        payload = ref[pos + 8:pos + size]
        if t == b'gps ':
            return b''
        if t in CONTAINERS:
            if t == b'trak':
                ctx = dict(ctx, kind=trak_kind(pos, size))
            inner = b''.join(rebuild(cp, cn, ctx) for _, cp, cn in children(ref, pos + 8, pos + size))
            return box(t, inner)
        kind = ctx.get('kind')
        tr = tracks.get(kind)
        if t == b'mvhd':
            p = bytearray(payload)
            struct.pack_into('>II', p, 4, ctime, ctime)
            struct.pack_into('>I', p, 16, movie_dur)
            return box(t, bytes(p))
        if t == b'tkhd':
            p = bytearray(payload)
            struct.pack_into('>II', p, 4, ctime, ctime)
            struct.pack_into('>I', p, 20, mdur[kind])
            return box(t, bytes(p))
        if t == b'mdhd':
            p = bytearray(payload)
            struct.pack_into('>II', p, 4, ctime, ctime)
            struct.pack_into('>I', p, 16, total[kind])
            return box(t, bytes(p))
        if t == b'stts':
            r = rle([x[3] for x in tr])
            return box(t, struct.pack('>II', 0, len(r)) + b''.join(struct.pack('>II', c, d) for c, d in r))
        if t == b'stss':
            idx = [i + 1 for i, x in enumerate(tr) if x[2]]
            return box(t, struct.pack('>II', 0, len(idx)) + b''.join(struct.pack('>I', i) for i in idx))
        if t == b'stsc':
            return box(t, struct.pack('>IIIII', 0, 1, 1, 1, 1))
        if t == b'stsz':
            return box(t, struct.pack('>III', 0, 0, len(tr)) + b''.join(struct.pack('>I', x[1]) for x in tr))
        if t == b'stco':
            if any(x[0] >= 1 << 32 for x in tr):
                die('file is larger than 4 GiB; 64-bit chunk offsets are not supported')
            return box(t, struct.pack('>II', 0, len(tr)) + b''.join(struct.pack('>I', x[0]) for x in tr))
        return ref[pos:pos + size]

    mp, mn = info['moov']
    return rebuild(mp, mn, {})


# --------------------------------------------------------------------------
def guess_creation_time(path, override):
    if override:
        return datetime.datetime.fromisoformat(override).replace(tzinfo=datetime.timezone.utc)
    m = re.match(r'(\d{14})', path.name)
    if m:
        return datetime.datetime.strptime(m.group(1), '%Y%m%d%H%M%S').replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('broken', type=Path, help='broken MP4 (missing moov)')
    ap.add_argument('--ref', type=Path, required=True, help='working MP4 from the same camera/settings')
    ap.add_argument('-o', '--out', type=Path, help='output file (default: <name>_recovered.MP4)')
    ap.add_argument('--creation-time', help='ISO time, e.g. 2026-10-22T05:16:28 (default: from file name)')
    ap.add_argument('--max-lost', type=int, default=300, help='max consecutive lost video samples per gap')
    ap.add_argument('--no-tail', action='store_true', help='do not try to recover frames after the last nidx box')
    args = ap.parse_args(argv)

    out = args.out or args.broken.with_name(args.broken.stem + '_recovered.MP4')
    if out.resolve() == args.broken.resolve():
        die('output must differ from the input')

    with open(args.broken, 'rb') as fh, open(args.ref, 'rb') as fr:
        b = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        ref = mmap.mmap(fr.fileno(), 0, access=mmap.ACCESS_READ)
        src = Source(b)
        if find_top_level(b, b'moov'):
            print('note: the file already has a moov atom; rebuilding anyway')
        mdat = find_top_level(b, b'mdat')
        if not mdat:
            die('no mdat box found')
        info = read_reference(ref)
        key, idx_boxes, video, audio = parse_indexes(b)
        print('index boxes: %d (%dx%d)  video samples: %d  audio samples: %d'
              % (len(idx_boxes), key[0], key[1], len(video), len(audio)))

        actual_v, gaps, dead_from = locate_video(src, video, args.max_lost)
        for zs, ze, d in gaps:
            print('gap detected: %d bytes missing (index offsets %d..%d)' % (d, zs, ze))
        if dead_from is not None:
            print('warning: data ends early; the rest of the indexed samples cannot be recovered')
        drop_dependents(video, actual_v)
        sig = learn_audio_signature(src, audio, gaps[0][0] if gaps else 1 << 62)
        actual_a = locate_audio(src, audio, gaps, dead_from, sig)

        vsamples = [(a, s, sync) for a, (_, s, sync) in zip(actual_v, video)]
        asamples = [(a, s, 0) for a, (_, s) in zip(actual_a, audio)]

        if not args.no_tail and dead_from is None and video and actual_v[-1] is not None:
            p0 = actual_v[-1] + video[-1][1]
            tail_start = None
            for p in range(p0, min(src.L, p0 + 65536)):
                if b[p + 4:p + 5] == b'\x65' and src.plausible_video(p, 20000):
                    tail_start = p
                    break
            if tail_start is not None:
                tv, ta = walk_tail(src, tail_start)
                print('unindexed tail: %d video + %d audio samples recovered' % (len(tv), len(ta)))
                vsamples += [(o, s, sync) for o, s, sync in tv]
                asamples += [(o, s, 0) for o, s in ta]

        vt, at = info['tracks'][b'vide'], info['tracks'][b'soun']
        tracks = {b'vide': build_track(vsamples, vt['dur']), b'soun': build_track(asamples, at['dur'])}
        for kind, tr in tracks.items():
            for o, s, _, _ in tr:
                assert 0 <= o and o + s <= src.L, 'sample outside file'
        if not tracks[b'vide']:
            die('no video samples could be recovered')
        print('video kept %d/%d (%.2f s)   audio kept %d/%d (%.2f s)' % (
            len(tracks[b'vide']), len(vsamples), sum(x[3] for x in tracks[b'vide']) / vt['ts'],
            len(tracks[b'soun']), len(asamples), sum(x[3] for x in tracks[b'soun']) / at['ts']))

        moov = make_moov(ref, info, tracks, guess_creation_time(args.broken, args.creation_time))
        mdat_pos = mdat[0]
        mdat_size = src.L - mdat_pos
        b.close()
        ref.close()

    shutil.copyfile(args.broken, out)
    with open(out, 'r+b') as f:
        f.seek(mdat_pos)
        if mdat_size < 1 << 32:
            f.write(struct.pack('>I', mdat_size))
        else:
            die('file is larger than 4 GiB; 64-bit mdat size not supported')
        f.seek(0, 2)
        f.write(moov)
    print('wrote', out, '(moov %d bytes)' % len(moov))


if __name__ == '__main__':
    main()

