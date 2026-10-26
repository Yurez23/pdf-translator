"""Readable generated references; no inference or PDF SDK."""

from .diagram_geometry import DiagramStructure


def references(structure: DiagramStructure) -> str:
    if structure.status=='correspondence':
        lines=['Diagram cell correspondences (explicit pointers):']
        for n in structure.nodes:
            if n.kind=='empty_cell':lines.append(f'{n.id}: empty source cell.')
            elif n.kind=='unresolved_cell':lines.append(f'{n.id}: graphical content unresolved; verify PDF.')
        lines.extend(f'{e.start} -> {e.end} (cell correspondence only).' for e in structure.connections)
        return '\n'.join(lines)
    lines = ["Diagram connections (partial; geometric links, not gameplay rules):"]
    for node in structure.nodes:
        if node.kind != "card":
            x,y,_,_ = node.source.bbox
            lines.append(f"{node.id}: {node.kind} at ({x:.2f}, {y:.2f}).")
    for edge in structure.connections:
        lines.append(f"{edge.start} -> {edge.end} (arrow direction only)." if edge.meaning=='directed'
                     else f"{edge.start} -- {edge.end} (undirected).")
    if not structure.connections:
        lines.append("No connections confidently recovered.")
    lines.append("Unmarked links have unknown direction; semantic conditions are not inferred. Unsupported structure may remain.")
    if "unconnected_card" in structure.issues:
        lines.append("Some cards have no recovered connection.")
    if "unresolved_endpoint" in structure.issues:
        lines.append("U references mark unresolved endpoints, not inferred cards.")
    return "\n".join(lines)


def annotate_cells(block):
    """Insert IDs at exact source-word offsets; source text stays unchanged."""
    text=block.text;offset=0;inserts=[];seen=set()
    for line in block.lines:
        for word in line.words:
            at=text.find(word.text,offset)
            if at<0:return text
            offset=at+len(word.text)
            x,y=(word.bbox[0]+word.bbox[2])/2,(word.bbox[1]+word.bbox[3])/2
            matches=[n for n in block.diagram.nodes if n.source.bbox[0]<x<n.source.bbox[2] and n.source.bbox[1]<y<n.source.bbox[3]]
            if len(matches)==1 and matches[0].id not in seen:
                inserts.append((at,matches[0].id));seen.add(matches[0].id)
    for at,ident in reversed(inserts):text=text[:at]+f'({ident}) '+text[at:]
    return text
