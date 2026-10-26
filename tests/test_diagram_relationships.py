"""Geometry oracles are specified by drawing commands, never recovered edges."""

from dataclasses import replace
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

import pdfplumber
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import Block, SourceRegion, convert_pdf
from pdf_translator.diagram_geometry import DiagramPath, recover_connections
from pdf_translator.diagram_mappings import recover_cell_mappings
from pdf_translator.export import render
from pdf_translator.models import Word
from pdf_translator.pdf_reader import _read_page
from tests.test_diagrams import PAGE, cards, draw_cards, fixture_pdf, walk_blocks


BOXES=[(10.,10.,90.,60.),(130.,10.,210.,60.),(10.,100.,90.,150.),(130.,100.,210.,150.)]
def path(*points,ident='a',width=1.,clip=None):return DiagramPath(ident,tuple(points),width,(0.,),clip)
def recover(paths,boxes=BOXES,**kwargs):return recover_connections(boxes,paths,page=1,size=10.,bounds=(0,0,250,200),**kwargs)
def edges(structure):return {frozenset((e.start,e.end)) for e in structure.connections}
def source_only(b):return replace(b,diagram=None,children=[source_only(c) for c in b.children])


class NeutralRelationships(unittest.TestCase):
    def test_duplicate_overpainted_clipped_paths_do_not_invent_branches(self):
        p=path((50,50),(50,110))
        s=recover([p,replace(p,id='b'),replace(p,id='c')],obstacles=[(20,75,80,95)])
        self.assertFalse(any(n.kind=='junction' for n in s.nodes))
        self.assertEqual(len(s.connections),2)
        self.assertEqual(len([n for n in s.nodes if n.kind=='unresolved']),2)

    def test_overlapping_trunk_with_bent_branch_keeps_actual_t_junction(self):
        s=recover([path((50,50),(50,110)),path((50,83),(50,70),(240,70),ident='b'),
                   path((50,83),(50,65),ident='c')],obstacles=[(20,75,80,95)])
        self.assertEqual(len([n for n in s.nodes if n.kind=='junction']),1)
        self.assertIn(frozenset(('N1','J1')),edges(s))
        self.assertIn(frozenset(('N3','U3')),edges(s))
        self.assertEqual(len(s.connections),4)

    def test_parallel_routes_keep_arrow_orientation_relative_to_same_nodes(self):
        s=recover([path((50,50),(50,110)),path((80,130),(110,130),(110,40),(80,40),ident='b')],
                  arrows=[path((107,75),(113,75),(110,82),ident='arrow')])
        self.assertEqual([(e.start,e.end,e.meaning) for e in s.connections],[('N1','N3','directed')])

    def test_interior_polyline_vertices_at_x_do_not_create_a_junction(self):
        boxes=[(10,70,40,100),(180,70,210,100),(100,10,130,40),(100,130,130,160)]
        s=recover([path((25,85),(115,85),(195,85)),path((115,25),(115,85),(115,145),ident='b')],boxes)
        self.assertEqual(edges(s),{frozenset(('N1','N4')),frozenset(('N2','N3'))})
        self.assertFalse(any(n.kind=='junction' for n in s.nodes))

    def test_branch_ending_on_two_crossing_shafts_is_ambiguous(self):
        boxes=[(10,70,40,100),(180,70,210,100),(100,10,130,40),(100,130,130,160),(10,10,40,40)]
        s=recover([path((25,85),(195,85)),path((115,25),(115,145),ident='b'),path((25,25),(115,85),ident='c')],boxes)
        self.assertIn('ambiguous_junction',s.issues)
        self.assertFalse(any(n.kind=='junction' for n in s.nodes))

    def test_independent_arrow_evidence_and_reverse_direction(self):
        for triangle,expected in [(((47,76),(53,76),(50,82)),('N1','N3')),
                                  (((47,82),(53,82),(50,76)),('N3','N1'))]:
            s=recover([path((50,50),(50,110))],arrows=[path(*triangle,ident='arrow')])
            e=s.connections[0]
            self.assertEqual((e.start,e.end,e.meaning),(*expected,'directed'))
            self.assertIn('arrow',{p.id for p in e.paths})

    def test_action_triangle_inside_card_is_not_a_connector_arrow(self):
        s=recover([path((50,50),(50,110))],arrows=[path((47,25),(53,25),(50,32),ident='icon')])
        self.assertEqual(s.connections[0].meaning,'undirected')

    def test_two_independent_diagrams_and_disconnected_duplicate_positions(self):
        boxes=[(10,10,90,60),(10,100,90,150),(150,10,230,60),(150,100,230,150)]
        s=recover([path((50,50),(50,110)),path((190,50),(190,110),ident='b')],boxes)
        self.assertEqual(edges(s),{frozenset(('N1','N3')),frozenset(('N2','N4'))})

    def test_simple_undirected_and_exact_route(self):
        s=recover([path((50,50),(50,115))])
        self.assertEqual(edges(s),{frozenset(('N1','N3'))})
        self.assertEqual(s.connections[0].meaning,'undirected')
        self.assertEqual(s.connections[0].route,(((50.,60.),(50.,100.)),))
        self.assertEqual(s.connections[0].paths[0].points,((50,50),(50,115)))

    def test_multisegment_bypass_does_not_connect_nearby_card(self):
        s=recover([path((80,40),(110,40),(110,130),(80,130))])
        self.assertEqual(edges(s),{frozenset(('N1','N3'))})

    def test_explicit_shared_trunk_keeps_junction_not_clique(self):
        s=recover([path((50,50),(50,80),(170,80),(170,110)),path((50,80),(50,110),ident='b')])
        self.assertEqual(edges(s),{frozenset(('N1','J1')),frozenset(('N3','J1')),frozenset(('N4','J1'))})
        self.assertEqual(next(n for n in s.nodes if n.id=='J1').source.bbox,(50,80,50,80))

    def test_x_crossing_is_not_junction(self):
        boxes=[(10,70,40,100),(180,70,210,100),(100,10,130,40),(100,130,130,160)]
        s=recover([path((25,85),(195,85)),path((115,25),(115,145),ident='b')],boxes)
        self.assertEqual(edges(s),{frozenset(('N1','N4')),frozenset(('N2','N3'))})
        self.assertIn('unjoined_crossing',s.issues)
        self.assertFalse(any(n.kind=='junction' for n in s.nodes))

    def test_small_fragment_gap_is_repaired_but_large_gap_is_not(self):
        for gap,joined in [(.3,True),(4,False)]:
            s=recover([path((50,50),(50,80)),path((50,80+gap),(50,110),ident='b')])
            self.assertEqual(frozenset(('N1','N3')) in edges(s),joined)
            if not joined:self.assertIn('unresolved_endpoint',s.issues)

    def test_different_stroke_styles_do_not_join(self):
        a=path((50,50),(50,80));b=replace(path((50,80),(50,110),ident='b'),color=(1.,))
        self.assertNotIn(frozenset(('N1','N3')),edges(recover([a,b])))

    def test_clip_cannot_create_invisible_link(self):
        s=recover([path((50,50),(50,110),clip=(0,0,250,85))])
        self.assertNotIn(frozenset(('N1','N3')),edges(s))
        self.assertIn('unresolved_endpoint',s.issues)

    def test_hidden_banner_severs_a_path(self):
        s=recover([path((50,50),(50,110))],obstacles=[(35,70,65,90)])
        self.assertNotIn(frozenset(('N1','N3')),edges(s))

    def test_frames_and_separators_do_not_create_links(self):
        paths=[path((10,10),(90,10),(90,60),(10,60),(10,10)),path((0,180),(240,180),ident='b')]
        self.assertEqual(recover(paths).connections,())

    def test_card_is_a_terminal_not_a_transparent_shortcut(self):
        boxes=[(10,10,90,60),(10,100,90,150),(10,190,90,240)]
        s=recover_connections(boxes,[path((50,40),(50,220))],page=1,size=10,bounds=(0,0,250,300))
        self.assertEqual(edges(s),{frozenset(('N1','N2')),frozenset(('N2','N3'))})

    def test_no_edges_from_alignment_or_from_nearby_endpoints(self):
        self.assertEqual(recover([]).connections,())
        s=recover([path((50,65),(50,95))])
        self.assertEqual(s.connections,())

    def test_input_order_and_numeric_noise(self):
        a=path((50,50),(50,80));b=path((50.03,80.02),(50,110),ident='b',width=1.001)
        self.assertEqual(recover([a,b]),recover([b,a]))
        self.assertIn(frozenset(('N1','N3')),edges(recover([a,b])))

    def test_scale_and_position(self):
        for scale in (.6,1.,2.):
            tr=lambda p:(p[0]*scale+30,p[1]*scale+20)
            boxes=[(*tr(b[:2]),*tr(b[2:])) for b in BOXES]
            s=recover_connections(boxes,[path(tr((50,50)),tr((50,110)))],page=3,size=10*scale,bounds=(0,0,600,600))
            self.assertEqual(edges(s),{frozenset(('N1','N3'))})
            self.assertTrue(all(n.source.page==3 for n in s.nodes))


class ExtractedRelationships(unittest.TestCase):
    def test_raster_cell_content_is_not_reported_as_empty(self):
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        def draw(c):
            c.setFont('Helvetica',9)
            for row in range(4):c.drawString(40,PAGE[1]-500-row*12,'Ordinary explanatory prose establishes the body size outside all cells.')
            for i in range(3):
                x=70+i*60
                for y in (100,150):c.rect(x,PAGE[1]-y-40,40,40,fill=0,stroke=1)
                c.setFont('Helvetica',20);c.drawString(x+10,PAGE[1]-130,str(i+1))
                p=c.beginPath();p.moveTo(x+15,PAGE[1]-142);p.lineTo(x+25,PAGE[1]-142);p.lineTo(x+20,PAGE[1]-148);p.close();c.drawPath(p,stroke=0,fill=1)
                if i==1:c.drawImage(ImageReader(Image.new('RGB',(10,10),'red')),x+10,PAGE[1]-180,10,10)
        payload=fixture_pdf(draw)
        with pdfplumber.open(BytesIO(payload)) as pdf:d=_read_page(pdf.pages[0],lambda _:None)
        self.assertEqual(len(d.diagram_mappings),1)
        self.assertEqual(next(n.kind for n in d.diagram_mappings[0].nodes if n.id=='C5'),'unresolved_cell')

    def test_form_xobject_paths_decline_without_assuming_its_clip_stack(self):
        def draw(c):
            c.beginForm('connectors',0,0,612,792);c.line(375,PAGE[1]-240,375,PAGE[1]-300);c.endForm()
            c.doForm('connectors');draw_cards(c,cards()[:4])
        r=self.convert(fixture_pdf(draw));s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertEqual(s.connections,())
        self.assertIn('unsupported_clip',s.issues)

    def test_source_positional_block_and_neutral_dependency_boundaries(self):
        import ast
        b=Block('paragraph','Original.',(SourceRegion(1,(0,0,1,1)),),0,[],[],10.,0.,'',None,[],0)
        self.assertIsNone(b.diagram)
        self.assertEqual(render([b]),('Original.\n','Original.\n'))
        root=Path(__file__).resolve().parents[1]/'pdf_translator'
        for name in ('diagram_geometry','diagram_mappings','diagram_export'):
            tree=ast.parse((root/(name+'.py')).read_text(encoding='utf8'))
            imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            imports += [a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
            self.assertFalse(any(i.startswith(('pdfplumber','pdfminer','google','reportlab')) for i in imports),imports)

    def test_extracted_arrow_is_not_inferred_from_vertical_position(self):
        def draw(c):
            c.setLineWidth(1);c.line(375,PAGE[1]-240,375,PAGE[1]-300)
            p=c.beginPath();p.moveTo(372,PAGE[1]-270);p.lineTo(378,PAGE[1]-270);p.lineTo(375,PAGE[1]-276);p.close()
            c.drawPath(p,stroke=0,fill=1);draw_cards(c,cards()[:4])
        r=self.convert(fixture_pdf(draw));s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertEqual([(e.start,e.end,e.meaning) for e in s.connections],[('N1','N3','directed')])
        self.assertIn('N1 -> N3 (arrow direction only).',r.text)

    def test_nonrectangular_clip_declines_invisible_geometry(self):
        def draw(c):
            c.saveState();p=c.beginPath();p.circle(375,PAGE[1]-260,10);c.clipPath(p,stroke=0,fill=0)
            c.line(375,PAGE[1]-240,375,PAGE[1]-300);c.restoreState();draw_cards(c,cards()[:4])
        r=self.convert(fixture_pdf(draw));s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertEqual(s.connections,())
        self.assertIn('unsupported_clip',s.issues)

    def test_synthetic_cell_mapping_extraction_and_both_exports(self):
        def draw(c):
            c.setFont('Helvetica',9)
            for row in range(4):c.drawString(40,PAGE[1]-500-row*12,'Ordinary explanatory prose remains outside the diagram and establishes body size.')
            for i in range(3):
                x=70+i*60
                for y in (100,150):c.rect(x,PAGE[1]-y-40,40,40,fill=0,stroke=1)
                c.setFont('Helvetica',20);c.drawString(x+10,PAGE[1]-130,str(i+1))
                if i!=1:c.drawString(x+8,PAGE[1]-180,str(i+7))
                p=c.beginPath();p.moveTo(x+15,PAGE[1]-142);p.lineTo(x+25,PAGE[1]-142);p.lineTo(x+20,PAGE[1]-148);p.close();c.drawPath(p,stroke=0,fill=1)
        r=self.convert(fixture_pdf(draw))
        structures=[b.diagram for b in walk_blocks(r.blocks) if b.diagram and b.diagram.status=='correspondence']
        self.assertEqual(len(structures),1)
        self.assertEqual([(e.start,e.end) for e in structures[0].connections],[('C1','C4'),('C2','C5'),('C3','C6')])
        self.assertIn('(C1) 1',r.text);self.assertIn('C5: empty source cell.',r.markdown)
        self.assertEqual(render(list(r.blocks)),(r.markdown,r.text))
        plain=render([source_only(b) for b in r.blocks])[1]
        self.assertIn('1 2 3',' '.join(plain.split()))
        self.assertNotIn('(C1)',plain)

    def convert(self,payload,pages=None):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'source.pdf';p.write_bytes(payload)
            return convert_pdf(p,pages=pages,hyphen_resolver=lambda _:False)

    def fixture(self,*,clip=False,duplicate=False):
        specs=cards()[:4]
        if duplicate:specs=[replace(c,title='Repeated Title') for c in specs]
        def draw(c):
            c.saveState()
            if clip:
                p=c.beginPath();p.rect(0,PAGE[1]-270,612,270);c.clipPath(p,stroke=0,fill=0)
            c.setLineWidth(1)
            p=c.beginPath();p.moveTo(375,PAGE[1]-240);p.lineTo(375,PAGE[1]-300);c.drawPath(p)
            c.restoreState();draw_cards(c,specs)
        return fixture_pdf(draw)

    def test_real_extraction_and_both_exports_with_single_text_ownership(self):
        r=self.convert(self.fixture());s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertEqual(edges(s),{frozenset(('N1','N3'))})
        self.assertIn('N1 -- N3 (undirected).',r.text)
        self.assertIn('N1 -- N3 (undirected).',r.markdown)
        self.assertEqual(render(list(r.blocks)),(r.markdown,r.text))
        self.assertEqual(r.text.count('Amber Vigil'),1)
        self.assertEqual(r.text.count('Prerequisite: Rank 1+'),1)
        self.assertEqual(len(s.connections[0].paths),1)

    def test_actual_rectangular_clip_and_graphics_state_restore(self):
        r=self.convert(self.fixture(clip=True));s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertNotIn(frozenset(('N1','N3')),edges(s))
        self.assertIn('unresolved_endpoint',s.issues)
        self.assertTrue(any(p.clip for e in s.connections for p in e.paths))

    def test_duplicate_titles_do_not_merge_nodes(self):
        r=self.convert(self.fixture(duplicate=True));s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertEqual(len([n for n in s.nodes if n.kind=='card']),4)
        self.assertEqual(r.text.count('Repeated Title'),4)
        self.assertEqual(edges(s),{frozenset(('N1','N3'))})

    def test_no_automatic_direction_for_upward_or_downward_strokes(self):
        r=self.convert(self.fixture());s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertTrue(all(e.meaning=='undirected' for e in s.connections))
        self.assertNotIn('prerequisite relationship',r.text)

    def test_selected_pages_have_no_other_page_warnings_links_or_text(self):
        stream=BytesIO();c=Canvas(stream,pagesize=PAGE,invariant=1)
        c.drawString(40,700,'First ordinary page only.');c.showPage()
        draw_cards(c,cards()[:4]);c.showPage();c.drawString(40,700,'Last ordinary page only.');c.save()
        r=self.convert(stream.getvalue(),pages='1,3')
        self.assertNotIn('Amber Vigil',r.text)
        self.assertFalse(any(w.code=='diagram_layout' for w in r.warnings))
        self.assertFalse(any(b.diagram for b in walk_blocks(r.blocks)))
        selected=self.convert(stream.getvalue(),pages='2')
        self.assertTrue(all(s.page==2 for b in walk_blocks(selected.blocks) for s in b.sources))
        self.assertTrue(all(set(w.pages)<={2} for w in selected.warnings))

    def test_raster_or_no_connector_falls_back_with_text(self):
        r=self.convert(fixture_pdf(lambda c:draw_cards(c,cards()[:4])))
        s=next(b.diagram for b in walk_blocks(r.blocks) if b.role=='diagram')
        self.assertEqual(s.connections,())
        self.assertIn('No connections confidently recovered.',r.text)
        self.assertTrue(any(w.code=='diagram_layout' for w in r.warnings))


class MappingRules(unittest.TestCase):
    def test_clipped_pointer_cannot_prove_a_mapping(self):
        cells,pointers,words=self.geometry()
        pointers[0]=replace(pointers[0],clip=(0,0,1,1))
        self.assertEqual(recover_cell_mappings(cells,pointers,words,[],page=1,size=10),[])

    def geometry(self,pointers=True):
        cells=[];triangles=[];words=[]
        for i in range(3):
            x=i*50+10;cells.extend([(x,10,x+40,50),(x,60,x+40,100)])
            words.append(Word(str(i+1),(x+10,20,x+20,35),10))
            if i!=1:words.append(Word(f'Result{i}',(x+5,70,x+35,85),10))
            if pointers:triangles.append(path((x+15,53),(x+25,53),(x+20,57),ident=str(i)))
        return cells,triangles,words

    def test_directed_correspondences_require_each_explicit_pointer(self):
        cells,pointers,words=self.geometry()
        structures=recover_cell_mappings(cells,pointers,words,[],page=2,size=10)
        self.assertEqual(len(structures),1)
        self.assertEqual([(e.start,e.end,e.meaning) for e in structures[0].connections],
                         [('C1','C4','correspondence'),('C2','C5','correspondence'),('C3','C6','correspondence')])
        self.assertEqual(next(n for n in structures[0].nodes if n.id=='C5').kind,'empty_cell')

    def test_alignment_alone_and_incomplete_pointer_rows_are_not_mappings(self):
        cells,_,words=self.geometry(False)
        self.assertEqual(recover_cell_mappings(cells,[],words,[],page=1,size=10),[])
        _,pointers,_=self.geometry()
        self.assertEqual(recover_cell_mappings(cells,pointers[:2],words,[],page=1,size=10),[])

    def test_unknown_marks_are_not_blank_outcomes(self):
        cells,pointers,words=self.geometry()
        s=recover_cell_mappings(cells,pointers,words,[(75,75,80,80)],page=1,size=10)[0]
        self.assertEqual(next(n for n in s.nodes if n.id=='C5').kind,'unresolved_cell')


class OptionalLocalSourceRelationships(unittest.TestCase):
    def test_heroic_root_overpaint_has_one_actual_header_junction(self):
        d=self.read(6,8)
        s=recover_connections(d.diagram_boxes,d.diagram_paths,page=8,size=d.body_size,bounds=(0,0,d.width,d.height),obstacles=d.diagram_obstacles,arrows=d.diagram_arrows)
        expected=[('N1','J1'),('J1','U1'),('J1','U2'),('U3','N2'),('U4','N3'),('N2','N4'),('N4','N6'),('N3','N5'),('N5','N7'),('N7','N9'),('N7','N8')]
        self.assertEqual(edges(s),{frozenset(p) for p in expected})
        self.assertEqual(len(s.connections),len(expected))
        junctions=[n for n in s.nodes if n.kind=='junction'];self.assertEqual(len(junctions),1)
        self.assertAlmostEqual(junctions[0].source.bbox[1],170.002)

    def read(self,n,p):
        matches=list((Path(__file__).resolve().parents[1]/'example').glob(f'{n}. *.pdf'))
        if not matches:self.skipTest('Optional local PDF not supplied')
        with pdfplumber.open(matches[0]) as pdf:return _read_page(pdf.pages[p-1],lambda _:None)

    def test_endeavors_exact_source_confirmed_web_with_junction(self):
        d=self.read(14,6)
        s=recover_connections(d.diagram_boxes,d.diagram_paths,page=6,size=d.body_size,bounds=(0,0,d.width,d.height),obstacles=d.diagram_obstacles,arrows=d.diagram_arrows)
        expected=[('N1','N4'),('N4','N7'),('N7','N10'),('N2','N5'),('N5','N8'),('N8','N11'),('N7','N8'),('N3','N6'),('N3','N9'),('N9','N12'),('N12','J1'),('J1','N13'),('N6','J1'),('N11','N13')]
        self.assertEqual(edges(s),{frozenset(p) for p in expected})
        self.assertTrue(all(e.meaning=='undirected' for e in s.connections))
        actual=next(n.source.bbox for n in s.nodes if n.id=='J1')
        self.assertLessEqual(abs(actual[0]-476.2),.3)
        self.assertEqual(actual[1],601.203)

    def test_heroic_agent_exact_visible_card_links_no_hidden_banner_shortcuts(self):
        d=self.read(6,9)
        s=recover_connections(d.diagram_boxes,d.diagram_paths,page=9,size=d.body_size,bounds=(0,0,d.width,d.height),obstacles=d.diagram_obstacles,arrows=d.diagram_arrows)
        expected=[('N1','N5'),('N2','N6'),('N6','N10'),('N9','N13'),('N9','N6'),('N6','N14'),('N3','N7'),('N7','N11'),('N7','N15'),('N4','N8'),('N8','N12'),('N8','N16')]
        actual={pair for pair in edges(s) if all(ident.startswith('N') for ident in pair)}
        self.assertEqual(actual,{frozenset(p) for p in expected})
        self.assertEqual({pair for pair in edges(s) if any(ident.startswith('U') for ident in pair)},
                         {frozenset(pair) for pair in [('U1','N1'),('U2','N2'),('U3','N3'),('U4','N4')]})
        self.assertTrue(all(e.meaning=='undirected' for e in s.connections))

    def test_plot_die_source_cells_with_exact_symbol_ownership(self):
        d=self.read(2,8)
        self.assertEqual(len(d.diagram_mappings),1)
        s=d.diagram_mappings[0]
        self.assertEqual([(e.start,e.end,e.meaning) for e in s.connections],[(f'C{i}',f'C{i+6}','correspondence') for i in range(1,7)])
        self.assertEqual([n.id for n in s.nodes if n.kind=='empty_cell'],['C9','C10'])
        cell_text=[]
        for node in s.nodes:
            box=node.source.bbox
            cell_text.append(' '.join(w.text for w in d.words if box[0]<(w.bbox[0]+w.bbox[2])/2<box[2] and box[1]<(w.bbox[1]+w.bbox[3])/2<box[3]))
        self.assertEqual(cell_text,['1','2','3','4','5','6','[complication] 2','[complication] 4','','','[opportunity]','[opportunity]'])


if __name__=='__main__':unittest.main()
