"""Explicit triangular pointers between repeated, separately framed cells."""

from .diagram_geometry import DiagramConnection, DiagramNode, DiagramStructure, _inside
from .models import SourceRegion


def recover_cell_mappings(cells, pointers, words, marks, *, page, size):
    """Require a visible pointer per pair; empty cell is not a gameplay rule."""
    cells=sorted(set(cells),key=lambda b:(b[1],b[0]))
    candidates=[]
    for pointer in pointers:
        points=list(dict.fromkeys(pointer.points))
        if len(points)!=3:
            continue
        if pointer.clip and not all(_inside(p,pointer.clip) for p in points):
            continue
        top=min(p[1] for p in points);bottom=max(p[1] for p in points)
        base=[p for p in points if abs(p[1]-top)<size*.03]
        tips=[p for p in points if abs(p[1]-bottom)<size*.03]
        if len(base)!=2 or len(tips)!=1 or bottom-top<size*.15:
            continue
        tip=tips[0]
        if not min(p[0] for p in base)<tip[0]<max(p[0] for p in base):
            continue
        above=[b for b in cells if b[0]<min(p[0] for p in base) and max(p[0] for p in base)<b[2] and 0<=top-b[3]<=size]
        below=[b for b in cells if b[0]<tip[0]<b[2] and 0<=b[1]-bottom<=size]
        if len(above)!=1 or len(below)!=1:
            continue
        a,b=above[0],below[0]
        if max(abs(a[0]-b[0]),abs(a[2]-b[2]))>size*.1:
            continue
        candidates.append((a,b,pointer))
    groups=[]
    for pair in candidates:
        for group in groups:
            if abs(group[0][0][1]-pair[0][1])<size*.1 and abs(group[0][1][1]-pair[1][1])<size*.1:
                group.append(pair);break
        else:groups.append([pair])
    results=[]
    for group in groups:
        if len(group)<3 or len({p[0] for p in group})!=len(group) or len({p[1] for p in group})!=len(group):
            continue
        if any(not any(_inside(((w.bbox[0]+w.bbox[2])/2,(w.bbox[1]+w.bbox[3])/2),a) for w in words) for a,_,_ in group):
            continue
        boxes=sorted({b for a,b,_ in group}|{a for a,b,_ in group},key=lambda b:(b[1],b[0]))
        nodes=[]
        for i,box in enumerate(boxes):
            content=[w for w in words if _inside(((w.bbox[0]+w.bbox[2])/2,(w.bbox[1]+w.bbox[3])/2),box)]
            ink=any(box[0]+size*.1<m[0] and m[2]<box[2]-size*.1 and box[1]+size*.1<m[1] and m[3]<box[3]-size*.1 for m in marks)
            nodes.append(DiagramNode(f"C{i+1}",SourceRegion(page,box),"cell" if content else "unresolved_cell" if ink else "empty_cell"))
        ids={node.source.bbox:node.id for node in nodes}
        edges=tuple(DiagramConnection(ids[a],ids[b],(p,),"correspondence",tuple(zip(p.points,p.points[1:]))) for a,b,p in sorted(group))
        results.append(DiagramStructure(tuple(nodes),edges,("cell_correspondence_only",),"correspondence"))
    return results


def attach_mappings(blocks, structures):
    """Annotate retained graphic-label words without moving source text."""
    from dataclasses import replace
    leaves=[]
    def visit(bs):
        for b in bs:
            if b.children:visit(b.children)
            else:leaves.append(b)
    visit(blocks)
    for structure in structures:
        owners=[b for b in leaves if b.kind!='table' and b.lines and any(
            _inside(((w.bbox[0]+w.bbox[2])/2,(w.bbox[1]+w.bbox[3])/2),n.source.bbox)
            for line in b.lines for w in line.words for n in structure.nodes)
            and all(any(_inside(((w.bbox[0]+w.bbox[2])/2,(w.bbox[1]+w.bbox[3])/2),n.source.bbox)
                        for n in structure.nodes) for line in b.lines for w in line.words)]
        covered={n.id for n in structure.nodes if any(_inside(((w.bbox[0]+w.bbox[2])/2,(w.bbox[1]+w.bbox[3])/2),n.source.bbox)
                    for b in owners for line in b.lines for w in line.words)}
        if not owners or any(n.kind=='cell' and n.id not in covered for n in structure.nodes):
            continue
        for b in owners:
            b.diagram=replace(structure,connections=(),status='cells')
            if b.kind=='heading':b.role='graphic_label'
        owners[-1].diagram=structure
