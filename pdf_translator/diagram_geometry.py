"""SDK-free, conservative topology of stroked paths between confirmed cards.

Crossings do not join unless a segment endpoint explicitly meets the other
segment. Cards occlude strokes: they terminate visible connections rather than
allowing a path to connect through several cards. No gameplay semantics.
"""

from dataclasses import dataclass, replace
from math import dist

from .models import BBox, SourceRegion

type Point = tuple[float, float]


@dataclass(frozen=True)
class DiagramPath:
    id: str
    points: tuple[Point, ...]
    width: float = 1.0
    color: tuple[float, ...] = ()
    clip: BBox | None = None


@dataclass(frozen=True)
class DiagramNode:
    id: str
    source: SourceRegion
    kind: str = "card"


@dataclass(frozen=True)
class DiagramConnection:
    start: str
    end: str
    paths: tuple[DiagramPath, ...]
    # Direction is deliberately absent for ordinary stroked connectors.
    meaning: str = "undirected"
    route: tuple[tuple[Point, Point], ...] = ()


@dataclass(frozen=True)
class DiagramStructure:
    nodes: tuple[DiagramNode, ...] = ()
    connections: tuple[DiagramConnection, ...] = ()
    issues: tuple[str, ...] = ()
    status: str = "partial"


def _inside(p, box, pad=0):
    return box[0]-pad <= p[0] <= box[2]+pad and box[1]-pad <= p[1] <= box[3]+pad


def _project(p, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    denominator = dx*dx+dy*dy
    t = max(0., min(1., ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/denominator)) if denominator else 0.
    q = (a[0]+t*dx, a[1]+t*dy)
    return t, q, dist(p, q)


def _cuts(a, b, box):
    """Line parameters at rectangle edges; no nearest-card inference."""
    values = []
    for axis, value in ((0, box[0]), (0, box[2]), (1, box[1]), (1, box[3])):
        delta = b[axis]-a[axis]
        if abs(delta) < 1e-8:
            continue
        t = (value-a[axis])/delta
        p = (a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1]))
        if 0 < t < 1 and _inside(p, box, 1e-6):
            values.append(t)
    return values


def _visible(a, b, boxes, clip):
    ts = sorted(set([0., 1., *_cuts(a, b, clip), *(t for box in boxes for t in _cuts(a, b, box))]))
    at = lambda t: (a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1]))
    for lo, hi in zip(ts, ts[1:]):
        midpoint = at((lo+hi)/2)
        if _inside(midpoint, clip) and not any(_inside(midpoint, box) for box in boxes):
            yield at(lo), at(hi)


def recover_connections(boxes: list[BBox], paths: list[DiagramPath], *, page: int,
                        size: float, bounds: BBox, obstacles: list[BBox] = (),
                        arrows: list[DiagramPath] = ()) -> DiagramStructure:
    """Keep actual visible paths, explicit T-junctions and unresolved stubs.

    Input boxes are confirmed card regions, not arbitrary nearby words. The
    tolerance scales with text size and only repairs touching endpoints. It
    never chooses a nearest card. Evidence includes every original path used.
    """
    boxes = sorted(set(boxes), key=lambda b: (b[1], b[0]))
    nodes = [DiagramNode(f"N{i+1}", SourceRegion(page, box)) for i, box in enumerate(boxes)]
    tolerance = max(.15, size*.12)
    segments = []
    issues = set()
    for path in sorted(paths,key=lambda p:p.id):
        if not path.points or len(path.points) < 2:
            continue
        clip = path.clip or bounds
        for a, b in zip(path.points, path.points[1:]):
            if dist(a, b) <= tolerance*.1:
                continue
            for x, y in _visible(a, b, boxes+list(obstacles), clip):
                # Ruling along a card edge is a frame, never a connection.
                if any((abs(x[0]-y[0]) < tolerance*.1 and
                        any(abs(x[0]-edge) < tolerance for edge in (box[0], box[2])) and
                        min(x[1],y[1]) < box[3] and max(x[1],y[1]) > box[1]) or
                       (abs(x[1]-y[1]) < tolerance*.1 and
                        any(abs(x[1]-edge) < tolerance for edge in (box[1], box[3])) and
                        min(x[0],y[0]) < box[2] and max(x[0],y[0]) > box[0]) for box in boxes):
                    continue
                if dist(x,y) > tolerance*.1:
                    segments.append((x,y,path))
    # Endpoints on a segment are explicit T meetings. Interior X crossings
    # retain their two independent routes, even if their coordinates coincide.
    ambiguous=[]
    for path in paths:
        for endpoint in (path.points[0],path.points[-1]) if path.points else ():
            shafts=[]
            for a,b,other in segments:
                if other.id==path.id or other.color!=path.color:continue
                t,_,gap=_project(endpoint,a,b)
                if gap<=tolerance and tolerance/dist(a,b)<t<1-tolerance/dist(a,b):
                    shafts.append((other.id,(b[0]-a[0],b[1]-a[1])))
            if any(i!=j and abs(u[0]*v[1]-u[1]*v[0])>dist((0,0),u)*dist((0,0),v)*.1
                   for i,u in shafts for j,v in shafts):
                ambiguous.append(endpoint);issues.add('ambiguous_junction')
    def ambiguous_point(point):return any(dist(point,p)<=tolerance for p in ambiguous)
    cuts = [[(0.,a),(1.,b)] for a,b,_ in segments]
    for i,(a,b,p) in enumerate(segments):
        for j,(c,d,q) in enumerate(segments):
            if i == j or p.color != q.color or abs(p.width-q.width) > max(.1,p.width*.15):
                continue
            for endpoint in (c,d):
                if p.id!=q.id and ambiguous_point(endpoint):continue
                parallel=abs((b[0]-a[0])*(d[1]-c[1])-(b[1]-a[1])*(d[0]-c[0])) < dist(a,b)*dist(c,d)*.03
                if p.id!=q.id and not parallel and all(dist(endpoint,v)>tolerance for v in (q.points[0],q.points[-1])):
                    # An internal bend/vertex is not an explicit branch end.
                    continue
                t, point, gap = _project(endpoint,a,b)
                if gap <= tolerance:
                    cuts[i].append((t,point))
            dx,dy=b[0]-a[0],b[1]-a[1];ex,ey=d[0]-c[0],d[1]-c[1]
            denominator=dx*ey-dy*ex
            if abs(denominator)>1e-8:
                t=((c[0]-a[0])*ey-(c[1]-a[1])*ex)/denominator
                u=((c[0]-a[0])*dy-(c[1]-a[1])*dx)/denominator
                if tolerance/dist(a,b)<t<1-tolerance/dist(a,b) and tolerance/dist(c,d)<u<1-tolerance/dist(c,d):
                    issues.add("unjoined_crossing")
    vertices = []
    adjacency = {}
    evidence = {}
    originals={path.id:path for path in paths}
    def directions(point,path):
        return [(b[0]-a[0],b[1]-a[1]) for a,b in zip(path.points,path.points[1:])
                if dist(a,b)>tolerance and _project(point,a,b)[2]<=tolerance]
    def vertex(point, path):
        # Complete-link clustering avoids bridging many small gaps into one.
        external=any(dist(point,p)<=tolerance for p in (path.points[0],path.points[-1]))
        for i,(points,style,members) in enumerate(vertices):
            collinear=any(abs(u[0]*v[1]-u[1]*v[0])<dist((0,0),u)*dist((0,0),v)*.03
                          for u in directions(point,path) for ident,_ in members
                          for v in directions(points[0],originals[ident]))
            meeting=(any(ident==path.id for ident,_ in members) or
                     collinear or
                     not ambiguous_point(point) and (external or any(end for _,end in members)))
            if meeting and style[0] == path.color and abs(style[1]-path.width)<=max(.1,path.width*.15) and all(dist(p,point)<=tolerance for p in points):
                points.append(point)
                members.append((path.id,external))
                return i
        vertices.append(([point],(path.color,round(path.width,3)),[(path.id,external)]))
        return len(vertices)-1
    for (a,b,path),split in zip(segments,cuts):
        split = sorted(split)
        for (_,x),(_,y) in zip(split,split[1:]):
            if dist(x,y) <= tolerance*.1:
                continue
            u,v=vertex(x,path),vertex(y,path)
            if u==v:
                continue
            adjacency.setdefault(u,set()).add(v);adjacency.setdefault(v,set()).add(u)
            evidence.setdefault(frozenset((u,v)),{})[path.id]=path
    anchors = {}
    for i,(points,_,_) in enumerate(vertices):
        point=points[0]
        matches=[]
        for node,box in zip(nodes,boxes):
            # Require a side contact away from corners. Near a frame corner
            # alone is insufficient evidence of a card connection.
            vertical=(box[1]+tolerance < point[1] < box[3]-tolerance and
                      min(abs(point[0]-box[0]),abs(point[0]-box[2]))<=tolerance)
            horizontal=(box[0]+tolerance < point[0] < box[2]-tolerance and
                        min(abs(point[1]-box[1]),abs(point[1]-box[3]))<=tolerance)
            if vertical or horizontal:
                matches.append(node.id)
        if len(matches)==1:
            anchors[i]=matches[0]
        elif matches:
            issues.add("ambiguous_card_contact")
    # Prune components without any card contact: legends/rules/artwork are
    # irrelevant to this graph. Retain unresolved stubs of anchored components.
    useful=set()
    for start in anchors:
        stack=[start]
        while stack:
            v=stack.pop()
            if v not in useful:
                useful.add(v);stack.extend(adjacency.get(v,()))
    terminals=dict(anchors)
    junctions=sorted((i for i in useful if i not in anchors and len(adjacency[i])!=2),
                     key=lambda i:(vertices[i][0][0][1],vertices[i][0][0][0],i))
    for i in junctions:
        point=vertices[i][0][0]
        if len(adjacency[i])>=3:
            ident=f"J{1+sum(n.kind=='junction' for n in nodes)}"; kind="junction"
        else:
            ident=f"U{1+sum(n.kind=='unresolved' for n in nodes)}"; kind="unresolved"
            issues.add("unresolved_endpoint")
        nodes.append(DiagramNode(ident,SourceRegion(page,(*point,*point)),kind))
        terminals[i]=ident
    connections=[];visited=set()
    for start in sorted(terminals):
        for nxt in sorted(adjacency.get(start,())):
            key=frozenset((start,nxt))
            if key in visited:
                continue
            route={};support=[];prev,current=start,nxt
            while True:
                key=frozenset((prev,current));visited.add(key);route.update(evidence[key])
                support.append((vertices[prev][0][0],vertices[current][0][0]))
                if current in terminals:
                    u,v=terminals[start],terminals[current]
                    if u!=v:
                        connections.append(DiagramConnection(u,v,tuple(route[k] for k in sorted(route)),route=tuple(support)))
                    break
                forward=adjacency.get(current,set())-{prev}
                if len(forward)!=1:
                    break
                prev,current=current,next(iter(forward))
    attached={e for c in connections for e in (c.start,c.end)}
    if any(n.kind=='card' and n.id not in attached for n in nodes):
        issues.add("unconnected_card")
    # A recovered geometric graph says nothing about banner associations,
    # prerequisites, artwork, or unextracted/raster relationships.
    issues.add("semantics_not_inferred")
    # Parallel visible routes between the same terminals share a reference;
    # their complete supporting segments remain in provenance.
    merged={}
    for edge in connections:
        key=tuple(sorted((edge.start,edge.end)))
        if key in merged:
            old=merged[key];support={p.id:p for p in old.paths+edge.paths}
            route=edge.route if edge.start==old.start else tuple((b,a) for a,b in reversed(edge.route))
            merged[key]=replace(old,paths=tuple(support[k] for k in sorted(support)),route=old.route+route)
        else:merged[key]=edge
    connections=[]
    for edge in merged.values():
        directions=set();support=[]
        for arrow in arrows:
            points=list(dict.fromkeys(arrow.points))
            if len(points)!=3:continue
            if arrow.clip and not all(_inside(p,arrow.clip) for p in points):continue
            for a,b in edge.route:
                for i,tip in enumerate(points):
                    base=[p for j,p in enumerate(points) if j!=i]
                    middle=((base[0][0]+base[1][0])/2,(base[0][1]+base[1][1])/2)
                    ta,_,da=_project(tip,a,b);tb,_,db=_project(middle,a,b)
                    width=dist(*base);length=dist(tip,middle)
                    # A symmetric triangle centred on an actual visible shaft
                    # supplies independent arrow evidence. Icons inside cards
                    # cannot match a visible route segment.
                    dot=(base[1][0]-base[0][0])*(b[0]-a[0])+(base[1][1]-base[0][1])*(b[1]-a[1])
                    if (size*.2<width<size*2 and width*.3<length<width*3
                            and abs(dot)<width*dist(a,b)*.08 and max(da,db)<tolerance*.5
                            and abs(ta-tb)*dist(a,b)>length*.9):
                        directions.add(1 if ta>tb else -1);support.append(arrow)
        if len(directions)==1:
            direction=next(iter(directions));paths={p.id:p for p in edge.paths+tuple(support)}
            edge=replace(edge,meaning='directed',paths=tuple(paths[k] for k in sorted(paths)),
                         start=edge.start if direction==1 else edge.end,end=edge.end if direction==1 else edge.start)
        elif directions:issues.add('conflicting_arrows')
        connections.append(edge)
    return DiagramStructure(tuple(nodes),tuple(connections),tuple(sorted(issues)))
