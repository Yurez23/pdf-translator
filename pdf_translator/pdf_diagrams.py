"""PDF adapter for linear paths and conservative clipping provenance."""

from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.utils import apply_matrix_pt

from .diagram_geometry import DiagramPath


class GeometryInterpreter(PDFPageInterpreter):
    """pdfminer ignores W/W*: keep rectangular clips, decline other clips.

    Clip metadata only affects relationship inference. Text, symbols and table
    extraction retain their established behavior. Form XObject clipping is
    conservatively unsupported for paths until its complete clip stack is known.
    """

    def get_current_state(self):
        state = super().get_current_state()
        state[2].diagram_clip = getattr(self.graphicstate,"diagram_clip",None)
        state[2].diagram_clip_unknown = getattr(self.graphicstate,"diagram_clip_unknown",False)
        return state

    def do_W(self):
        points=[]
        for operation in self.curpath:
            if operation[0] in ("m","l"):
                points.append(apply_matrix_pt(self.ctm,(operation[1],operation[2])))
            elif operation[0]!="h":
                self.graphicstate.diagram_clip_unknown=True
                return
        points=list(dict.fromkeys(points))
        xs,ys=sorted({p[0] for p in points}),sorted({p[1] for p in points})
        if len(points)!=4 or len(xs)!=2 or len(ys)!=2:
            self.graphicstate.diagram_clip_unknown=True
            return
        box=(xs[0],ys[0],xs[1],ys[1]);old=getattr(self.graphicstate,"diagram_clip",None)
        if old:
            box=(max(box[0],old[0]),max(box[1],old[1]),min(box[2],old[2]),min(box[3],old[3]))
        self.graphicstate.diagram_clip=box

    do_W_a=do_W


def extract_paths(page):
    """Return neutral linear strokes; never approximate curves by their bbox."""
    paths=[];issues=set()
    for index,shape in enumerate((*page.lines,*page.curves,*page.rects)):
        if not shape.get("stroke") or shape.get("fill"):
            continue
        if shape.get("diagram_clip_unknown"):
            issues.add("unsupported_clip");continue
        operations=shape.get("path",())
        points=[];subpaths=[];valid=True
        for operation in operations:
            op=operation[0]
            if op=="m":
                if points:subpaths.append(points)
                points=[tuple(operation[1])]
            elif op=="l" and points:
                points.append(tuple(operation[1]))
            elif op=="h" and points:
                points.append(points[0])
            else:
                valid=False;break
        if not valid:
            issues.add("unsupported_curved_path");continue
        if points:subpaths.append(points)
        color=shape.get("stroking_color",())
        color=(color,) if isinstance(color,(int,float)) else tuple(color or ())
        for subindex,points in enumerate(subpaths):
            paths.append(DiagramPath(f"p{page.page_number}-s{index}-{subindex}",tuple(points),
                                     float(shape.get("linewidth",1)),color,shape.get("diagram_clip")))
    return paths,tuple(sorted(issues))


def extract_mapping_geometry(page, size):
    cells=[];pointers=[]
    marks=[tuple(i[k] for k in ('x0','top','x1','bottom')) for i in page.images]
    for i,s in enumerate((*page.rects,*page.curves)):
        box=tuple(s[k] for k in ('x0','top','x1','bottom'))
        width,height=box[2]-box[0],box[3]-box[1]
        if s.get('diagram_clip_unknown'):
            marks.append(box);continue
        clip=s.get('diagram_clip')
        if clip and not (clip[0]<=box[0] and clip[1]<=box[1] and box[2]<=clip[2] and box[3]<=clip[3]):
            marks.append(box);continue
        if s['object_type']=='rect' and s.get('stroke') and size*1.5<width<size*8 and .75<width/max(height,.01)<1.25:
            if not any(max(abs(a-b) for a,b in zip(box,other))<size*.05 for other in cells):cells.append(box)
        points=list(dict.fromkeys(tuple(p) for p in s.get('pts',())))
        if (s.get('fill') and len(points)==3 and size*.3<width<size*2 and size*.15<height<size
                and all(op[0] in ('m','l','h') for op in s.get('path',()))):
            pointers.append(DiagramPath(f'p{page.page_number}-pointer{i}',tuple(points),0.,(),s.get('diagram_clip')))
        color=s.get('non_stroking_color')
        white=isinstance(color,(list,tuple)) and len(color)==3 and min(color)>=.98
        if not (s['object_type']=='rect' and (s.get('stroke') or white)):
            marks.append(box)
    return cells,pointers,marks


def extract_arrows(page, size):
    arrows=[]
    for i,s in enumerate(page.curves):
        pts=tuple(dict.fromkeys(tuple(p) for p in s.get('pts',())))
        if (s.get('fill') and not s.get('diagram_clip_unknown') and len(pts)==3
                and all(op[0] in ('m','l','h') for op in s.get('path',()))
                and max(s['x1']-s['x0'],s['bottom']-s['top'])<size*2):
            arrows.append(DiagramPath(f'p{page.page_number}-arrow{i}',pts,0.,(),s.get('diagram_clip')))
    return arrows
