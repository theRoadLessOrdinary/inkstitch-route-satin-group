#!/usr/bin/env python3
"""
Route Satin Group (Ink/Stitch satin column helper)

Select one group. Each group directly inside it is one layer of stitching,
sewn in its own turn and never mixed with the others; a group with no groups
inside is one layer by itself. Only satin columns are routed.

For every column, in stitch order:
  - both rails are made to point the same way, so the column finishes at the
    end nearest the start of the next column;
  - the move to the next column must stay inside the area all the columns
    cover together, and must not cross anything already stitched. When a
    straight jump can't, the first of these that works is used:
      1. a travel path (running stitch) from the end of the column, over
         area not yet stitched, to where a straight jump becomes possible;
      2. a different order of the columns inside the layer;
      3. a trim after the column.

Travel paths and trims from an earlier run are removed first and made again.
"""
import hashlib
import heapq
import json
import math
import os
import tempfile

import numpy as np
import inkex
from inkex import EffectExtension, PathElement, Group, CubicSuperPath
from lxml import etree

from shapely.geometry import Polygon, LineString, Point
from shapely.ops import unary_union
from shapely.validation import make_valid
from shapely import prepared, contains_xy

INKSTITCH = "http://inkstitch.org/namespace"
MAPSEDGE = "https://mapsedgecreative.com/ns/inkscape"
etree.register_namespace("mapsedge", MAPSEDGE)
ROUTE_ATTR = f"{{{MAPSEDGE}}}route"
SATIN_ATTR = f"{{{INKSTITCH}}}satin_column"
TRIM_ATTR = f"{{{INKSTITCH}}}trim_after"

GAP_MM = 0.25          # a gap between columns narrower than twice this counts as inside
END_CLEAR_MM = 0.5     # a move may touch stitched area this close to where it leaves or lands
TRAVEL_CLEAR_MM = 0.2  # travel keeps this far inside the area's edge
TRAVEL_MARK_MM = 0.15  # half the width a stitched travel line takes up
GRID_MM = 0.25         # cell size of the travel search
SAMPLE_MM = 0.2        # rail sampling step


def bezier_points(sp, step):
    """Points along one subpath, about `step` apart."""
    out = [np.array(sp[0][1], dtype=float)]
    for k in range(1, len(sp)):
        p0, p1 = np.array(sp[k - 1][1]), np.array(sp[k - 1][2])
        p2, p3 = np.array(sp[k][0]), np.array(sp[k][1])
        approx = np.linalg.norm(p1 - p0) + np.linalg.norm(p2 - p1) + np.linalg.norm(p3 - p2)
        n = max(1, int(math.ceil(approx / step)))
        t = np.linspace(0, 1, n + 1)[1:, None]
        u = 1 - t
        out.extend(u ** 3 * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t ** 3 * p3)
    return np.array(out)


def reverse_subpath(subpath):
    """The same subpath traced the other way: nodes reversed, and each node's
    two handles swapped."""
    return [[list(n[2]), list(n[1]), list(n[0])] for n in reversed(subpath)]


def make_csp(subpaths):
    obj = CubicSuperPath.__new__(CubicSuperPath)
    list.__init__(obj, subpaths)
    return obj


def find_rail_indices(csp):
    """The two rails: a rung is never longer than a rail, so the rails are the
    two longest subpaths. Returned in their order in the path."""
    lengths = [float(np.sum(np.linalg.norm(np.diff(bezier_points(sp, 0.5), axis=0), axis=1)))
               for sp in csp]
    order = sorted(range(len(csp)), key=lambda i: lengths[i], reverse=True)
    return sorted(order[:2])


class Column:
    """One satin column: its element, rail ends (document coordinates) and area."""

    def __init__(self, el, step):
        self.el = el
        self.id = el.get_id()
        self.csp = [list(sp) for sp in el.path.to_absolute().to_superpath()]
        self.ia, self.ib = find_rail_indices(self.csp)
        (a, c, e), (b, d, f) = el.composed_transform().matrix
        doc = lambda P: np.column_stack([a * P[:, 0] + c * P[:, 1] + e, b * P[:, 0] + d * P[:, 1] + f])
        A = doc(bezier_points(self.csp[self.ia], step))
        B = doc(bezier_points(self.csp[self.ib], step))
        self.b_flip = bool(np.linalg.norm(A[0] - B[-1]) + np.linalg.norm(A[-1] - B[0]) <
                           np.linalg.norm(A[0] - B[0]) + np.linalg.norm(A[-1] - B[-1]))
        if self.b_flip:
            B = B[::-1]
        self.ends = ((A[0] + B[0]) / 2, (A[-1] + B[-1]) / 2)
        area = make_valid(Polygon(np.vstack([A, B[::-1]])))
        self.area = unary_union([g for g in getattr(area, "geoms", [area])
                                 if g.geom_type in ("Polygon", "MultiPolygon")])
        self.mask = None

    def start(self, d):
        """d = 0: the way rail A runs now; 1: reversed."""
        return self.ends[d]

    def end(self, d):
        return self.ends[1 - d]

    def write(self, d):
        """Both rails pointing way d. True if the path changed."""
        a = self.csp[self.ia]
        b = reverse_subpath(self.csp[self.ib]) if self.b_flip else self.csp[self.ib]
        if d:
            a, b = reverse_subpath(a), reverse_subpath(b)
        csp = list(self.csp)
        csp[self.ia], csp[self.ib] = a, b
        if csp == self.csp:
            return False
        self.el.path = make_csp(csp).to_path()
        return True


class Grid:
    """The area as cells, for the travel search."""

    def __init__(self, region, region_in, cell):
        self.cell = cell
        self.minx, self.miny, maxx, maxy = region.bounds
        self.nx = int((maxx - self.minx) / cell) + 2
        self.ny = int((maxy - self.miny) / cell) + 2
        xs = self.minx + (np.arange(self.nx) + 0.5) * cell
        ys = self.miny + (np.arange(self.ny) + 0.5) * cell
        self.xs, self.ys = xs, ys
        self.X, self.Y = np.meshgrid(xs, ys)
        self.region = contains_xy(region, self.X, self.Y)
        self.inside = contains_xy(region_in, self.X, self.Y)

    def mask_of(self, geom):
        return contains_xy(geom, self.X, self.Y)

    def disk(self, p, r):
        return (self.X - p[0]) ** 2 + (self.Y - p[1]) ** 2 <= r * r

    def cell_of(self, p):
        return (min(self.ny - 1, max(0, int((p[1] - self.miny) / self.cell))),
                min(self.nx - 1, max(0, int((p[0] - self.minx) / self.cell))))

    def clear_line(self, ok, p, q):
        n = max(2, int(math.hypot(q[0] - p[0], q[1] - p[1]) / (self.cell * 0.5)) + 1)
        for t in np.linspace(0, 1, n):
            r, c = self.cell_of((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t))
            if not ok[r, c]:
                return False
        return True

    def route(self, ok, a, b):
        """Shortest way from a to b over cells marked ok, pulled taut:
        a list of points from a to b, or None."""
        s, g = self.cell_of(a), self.cell_of(b)
        if not (ok[s] and ok[g]):
            return None
        steps = [(dr, dc, math.hypot(dr, dc)) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc]
        dist = {s: 0.0}
        prev = {}
        heap = [(0.0, 0.0, s)]
        while heap:
            _, d, q = heapq.heappop(heap)
            if q == g:
                break
            if d > dist[q]:
                continue
            for dr, dc, w in steps:
                r, c = q[0] + dr, q[1] + dc
                if 0 <= r < self.ny and 0 <= c < self.nx and ok[r, c]:
                    nd = d + w
                    if nd < dist.get((r, c), 1e18):
                        dist[(r, c)] = nd
                        prev[(r, c)] = q
                        heapq.heappush(heap, (nd + math.hypot(r - g[0], c - g[1]), nd, (r, c)))
        if g not in dist:
            return None
        cells = [g]
        while cells[-1] != s:
            cells.append(prev[cells[-1]])
        pts = [tuple(a)] + [(self.xs[c], self.ys[r]) for r, c in reversed(cells)] + [tuple(b)]
        out = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1 and not self.clear_line(ok, pts[i], pts[j]):
                j -= 1
            out.append(pts[j])
            i = j
        return [np.array(p) for p in out]


class Router:
    def __init__(self, cols, mm):
        self.mm = mm
        self.by_id = {c.id: c for c in cols}
        region = unary_union([c.area for c in cols])
        self.region_geom = region.buffer(GAP_MM * mm).buffer(-GAP_MM * mm)
        self.region = prepared.prep(self.region_geom.buffer(0.01 * mm))
        self.region_in = self.region_geom.buffer(-TRAVEL_CLEAR_MM * mm)
        self.grid = Grid(self.region_geom, self.region_in, GRID_MM * mm)
        for c in cols:
            c.mask = self.grid.mask_of(c.area.buffer(GRID_MM * mm * 0.75))
            c.shrunk = c.area.buffer(-0.01 * mm)
        self.unions = {}
        self.clear = END_CLEAR_MM * mm
        self.memo = {}
        self.marks = {}

    def mark(self, line):
        """The area a stitched travel line takes up, and its cells."""
        if id(line) not in self.marks:
            m = LineString(line).buffer(TRAVEL_MARK_MM * self.mm)
            self.marks[id(line)] = (m, self.grid.mask_of(m.buffer(GRID_MM * self.mm * 0.75)), line)
        return self.marks[id(line)]

    @staticmethod
    def directions(seq, last_dir):
        """Backward: each column ends at its end nearest the next one's start."""
        dirs = [0] * len(seq)
        dirs[-1] = last_dir
        for i in range(len(seq) - 2, -1, -1):
            nxt = seq[i + 1].start(dirs[i + 1])
            c = seq[i]
            dirs[i] = 0 if np.linalg.norm(c.ends[1] - nxt) <= np.linalg.norm(c.ends[0] - nxt) else 1
        return dirs

    def move(self, done, travels, c, dc, n, dn):
        """How to get from the end of c to the start of n with `done` columns
        and `travels` lines already stitched: ('jump',), ('travel', points)
        or ('trim',)."""
        key = (done, tuple(map(id, travels)), c.id, dc, n.id, dn)
        if key in self.memo:
            return self.memo[key]
        a, b = c.end(dc), n.start(dn)
        seg = LineString([a, b])
        ends = Point(a).buffer(self.clear).union(Point(b).buffer(self.clear))
        marks = [self.mark(t) for t in travels]
        stitched = self.stitched(done, travels, marks)
        part = seg.difference(ends)
        if self.region.covers(seg) and (part.is_empty or not part.intersects(stitched)):
            res = ("jump",)
        elif not self.connected(stitched, a, b):
            res = ("trim",)
        else:
            g = self.grid
            busy = np.zeros_like(g.inside)
            for i in done:
                busy |= self.by_id[i].mask
            for m in marks:
                busy |= m[1]
            ok = (g.inside & ~busy) | ((g.disk(a, self.clear) | g.disk(b, self.clear)) & g.region)
            route = g.route(ok, a, b)
            if route is None:
                res = ("trim",)
            elif len(route) == 2:
                res = ("jump",)
            else:
                res = ("travel", route[:-1])
        self.memo[key] = res
        return res

    def stitched(self, done, travels, marks):
        """Everything stitched so far, a hair inside its edges. Built from the
        largest set already built that it contains."""
        key = (done, tuple(map(id, travels)))
        if key not in self.unions:
            base, have = None, (frozenset(), ())
            for (d, tr), u in self.unions.items():
                if d <= done and set(tr) <= set(key[1]) and len(d) + len(tr) > len(have[0]) + len(have[1]):
                    base, have = u, (d, tr)
            parts = [self.by_id[i].shrunk for i in done - have[0]]
            parts += [m[0] for m in marks if id(m[2]) not in have[1]]
            self.unions[key] = unary_union(([base] if base is not None else []) + parts)
        return self.unions[key]

    def connected(self, stitched, a, b):
        """Whether one piece of the free area reaches both a and b."""
        da, db = Point(a).buffer(self.clear), Point(b).buffer(self.clear)
        free = self.region_in.difference(stitched)
        free = unary_union([free, da.intersection(self.region_geom), db.intersection(self.region_geom)])
        return any(p.intersects(da) and p.intersects(db) for p in getattr(free, "geoms", [free]))

    def evaluate(self, seq, dirs, kept, prefix=()):
        """(trims, travels, length, moves): moves[i] is the move after seq[i].
        The first moves are taken from prefix as they are."""
        trims = travels = 0
        length = 0.0
        moves = []
        done = frozenset()
        lines = ()
        for i in range(len(seq) - 1):
            c, n = seq[i], seq[i + 1]
            done = done | {c.id}
            if i < len(prefix):
                m = prefix[i]
            elif c.id in kept:
                m = ("trim",)
            else:
                m = self.move(done, lines, c, dirs[i], n, dirs[i + 1])
            moves.append(m)
            if m[0] == "trim":
                trims += 1
                continue
            length += float(np.linalg.norm(n.start(dirs[i + 1]) - c.end(dirs[i])))
            if m[0] == "travel":
                travels += 1
                lines = lines + (m[1],)
        return trims, travels, length, moves


def displacement(order, original):
    pos = {c.id: i for i, c in enumerate(original)}
    return sum(abs(pos[c.id] - i) for i, c in enumerate(order))


def fingerprint(col):
    """The column's layer-independent shape, rounded so a save and reload
    in Inkscape doesn't read as a change."""
    (a, c, e), (b, d, f) = col.el.composed_transform().matrix
    pts = [round(v, 3) for sp in col.csp for node in sp for p in node for v in p]
    return hashlib.sha1(json.dumps([pts, [round(v, 6) for v in (a, b, c, d, e, f)]]).encode()).hexdigest()


def cache_path():
    return os.path.join(tempfile.gettempdir(), "maps_edge_route_cache.json")


def load_cache(key):
    try:
        with open(cache_path()) as f:
            return json.load(f).get(key)
    except (OSError, ValueError):
        return None


def save_cache(key, entry):
    try:
        with open(cache_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[key] = entry
    try:
        with open(cache_path(), "w") as f:
            json.dump(data, f)
    except OSError:
        pass


class RouteSatinGroup(EffectExtension):
    def effect(self):
        # One group, perhaps selected along with groups inside it; or several
        # groups side by side, each a layer.
        groups = [e for e in self.svg.selection if isinstance(e, Group)]
        outer = [g for g in groups if not any(a in groups for a in g.iterancestors())]
        if not outer:
            raise inkex.AbortExtension("Select the group that holds the satin columns.")
        if len(outer) == 1:
            scope = outer
            layer_groups = [g for g in outer[0] if isinstance(g, Group)] or outer
        elif len({g.getparent() for g in outer}) == 1:
            parent = outer[0].getparent()
            scope = layer_groups = sorted(outer, key=parent.index)
        else:
            raise inkex.AbortExtension("Select one group (its sub-groups are its layers), "
                                       "or several groups that sit side by side in one group.")
        mm = self.svg.viewport_to_unit("1mm")

        # start clean: no travel paths from an earlier run, no trims at all
        for el in [x for g in scope for x in g.iter()]:
            if el.get(ROUTE_ATTR) == "travel":
                el.getparent().remove(el)
            else:
                el.attrib.pop(TRIM_ATTR, None)
                el.attrib.pop(ROUTE_ATTR, None)

        layers = [[Column(el, SAMPLE_MM * mm) for el in g
                   if isinstance(el, PathElement) and el.get(SATIN_ATTR) == "True"]
                  for g in layer_groups]
        layer_of = {c.id: li for li, L in enumerate(layers) for c in L}
        layers = [L for L in layers if L]
        allcols = [c for L in layers for c in L]
        if len(allcols) < 2:
            raise inkex.AbortExtension("The group needs at least two satin columns.")
        kept = set()

        router = Router(allcols, mm)
        original = [list(L) for L in layers]
        prints = {c.id: fingerprint(c) for c in allcols}

        # An earlier run in this session on the same group: its result holds
        # up to the first column that is new, gone or changed.
        key = f"{self.svg.get('sodipodi:docname') or ''}#{','.join(g.get_id() for g in scope)}"
        cached = load_cache(key)
        reuse = 0
        reused = [tuple([m[0]] + ([[np.array(p) for p in m[1]]] if m[0] == "travel" else []))
                  for m in cached["moves"]] if cached else []
        if cached:
            cur = [c for L in layers for c in L]
            k = 0
            while (k < len(cur) and k < len(cached["seq"]) and cur[k].id == cached["seq"][k]
                   and prints[cur[k].id] == cached["prints"][k] and layer_of[cur[k].id] == cached["layer"][k]):
                k += 1
            reuse = max(0, k - 1)
            # When any column changed, the shape did too. A jump is reused only
            # if it still lies inside it; a travel or trim only if the same
            # move would be chosen now (a bigger shape can make it needless).
            if sorted(prints.values()) != cached["all"]:
                done, lines = frozenset(), ()
                for i in range(reuse):
                    m = reused[i]
                    da, db = cached["dirs"][i], cached["dirs"][i + 1]
                    done = done | {cur[i].id}
                    if m[0] == "jump":
                        line = LineString([cur[i].end(da), cur[i + 1].start(db)])
                        same = router.region.covers(line)
                    else:
                        now = router.move(done, lines, cur[i], da, cur[i + 1], db)
                        same = now[0] == m[0] and (m[0] == "trim" or (
                            len(now[1]) == len(m[1]) and
                            all(np.linalg.norm(p - q) < 0.05 * mm for p, q in zip(now[1], m[1]))))
                    if not same:
                        reuse = i
                        break
                    if m[0] == "travel":
                        lines = lines + (m[1],)

        def prefix(n):
            return reused[:n]

        def score(lays, n):
            seq = [c for L in lays for c in L]
            dirs = router.directions(seq, 0)
            t, tr, ln, mv = router.evaluate(seq, dirs, kept, prefix(n))
            disp = sum(displacement(L, O) for L, O in zip(lays, original))
            return (t, disp, tr, ln), seq, dirs, mv

        def search(n):
            """Wherever a move from position n on needs a trim, try bringing
            each later column of the same layer forward to follow it; keep the
            change only when it means fewer trims overall. Layers in order,
            earlier layers settled."""
            lays = [list(L) for L in layers]
            best = score(lays, n)
            offset = 0
            for li in range(len(lays)):
                pos = max(0, n - offset)
                while pos < len(lays[li]):
                    i = offset + pos
                    if i >= len(best[3]) or best[3][i][0] != "trim":
                        pos += 1
                        continue
                    pick = None
                    for j in range(pos + 2, len(lays[li])):
                        trial = [list(L) for L in lays]
                        trial[li].insert(pos + 1, trial[li].pop(j))
                        s = score(trial, n)
                        if s[0] < best[0] and (pick is None or s[0] < pick[1][0]):
                            pick = (trial, s)
                    if pick:
                        lays, best = pick
                    else:
                        pos += 1
                offset += len(lays[li])
            return lays, best

        while True:
            lays, best = search(reuse)
            dirs = best[2]
            # the reused moves are only right if the columns before them still
            # run the way they did
            bad = next((i for i in range(min(reuse + 1, len(dirs))) if dirs[i] != cached["dirs"][i]), None) if reuse else None
            if bad is None:
                break
            reuse = max(0, bad - 1)
        layers = lays
        (trims, _, travels, _), seq, dirs, moves = best

        flipped = sum(1 for d in dirs if d)
        matched = sum(1 for c in seq if c.b_flip)
        for c, d in zip(seq, dirs):
            c.write(d)
        moved = 0
        for L, O in zip(layers, original):
            slots = [(c.el.getparent(), c.el.getparent().index(c.el)) for c in O]
            moved += sum(1 for a, b in zip(L, O) if a is not b)
            for c in O:
                c.el.getparent().remove(c.el)
            for (parent, idx), c in zip(slots, L):
                parent.insert(idx, c.el)
        added = []
        for i, m in enumerate(moves):
            c = seq[i]
            if m[0] == "travel":
                added.append(self.add_travel(c, m[1]))
            elif m[0] == "trim":
                c.el.set(TRIM_ATTR, "True")
                c.el.set(ROUTE_ATTR, "trim")

        # what this run left, for the next one; directions now all 0
        for c in seq:
            c.csp = [list(sp) for sp in c.el.path.to_absolute().to_superpath()]
        save_cache(key, {"seq": [c.id for c in seq], "layer": [layer_of[c.id] for c in seq],
                         "prints": [fingerprint(c) for c in seq], "dirs": [0] * len(seq),
                         "all": sorted(fingerprint(c) for c in seq),
                         "moves": [[m[0]] + ([[list(map(float, p)) for p in m[1]]] if m[0] == "travel" else [])
                                   for m in moves]})

        trimmed = [seq[i].id for i, m in enumerate(moves) if m[0] == "trim"]
        if reuse == len(seq) - 1:
            self.msg("Nothing changed since the last run; the result is the same.")
        elif reuse:
            self.msg(f"Kept the earlier result up to {seq[reuse].id}; worked out the rest again.")
        self.msg(f"{len(allcols)} satin columns in {len(layers)} layers.")
        self.msg(f"Columns reversed: {flipped}. Columns whose rails pointed opposite ways, now matched: {matched}.")
        self.msg(f"Columns in a new place in the order: {moved}.")
        self.msg(f"Travel paths added: {len(added)}" + (f" ({', '.join(added)})" if added else "") + ".")
        self.msg(f"Trims: {len(trimmed)}" + (f", after {', '.join(trimmed)}" if trimmed else "") + ".")

        if os.environ.get("ROUTE_DEBUG"):
            with open(os.environ["ROUTE_DEBUG"], "w") as f:
                json.dump({"region": router.region_geom.wkt,
                           "cols": [[{"id": c.id, "area": c.area.wkt, "ends": [list(map(float, e)) for e in c.ends]}
                                     for c in L] for L in layers],
                           "seq": [c.id for c in seq], "dirs": dirs,
                           "moves": [[m[0]] + ([[list(map(float, q)) for q in m[1]]] if m[0] == "travel" else [])
                                     for m in moves]}, f)

    def add_travel(self, col, pts):
        """A running stitch path along pts, right after col in its group."""
        parent = col.el.getparent()
        inv = -parent.composed_transform()
        nodes = [inv.apply_to_point((float(q[0]), float(q[1]))) for q in pts]
        el = PathElement()
        el.set("d", "M " + " L ".join(f"{p.x:.4f},{p.y:.4f}" for p in nodes))
        el.style = {"fill": "none", "stroke": col.el.style.get("stroke") or "#000000",
                    "stroke-width": f"{0.1 * self.svg.viewport_to_unit('1mm'):.4f}"}
        el.set(ROUTE_ATTR, "travel")
        el.set("inkscape:label", f"Travel after {col.id}")
        parent.insert(parent.index(col.el) + 1, el)
        return el.get_id()


if __name__ == "__main__":
    RouteSatinGroup().run()
