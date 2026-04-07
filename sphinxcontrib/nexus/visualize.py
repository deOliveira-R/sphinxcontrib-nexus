"""Interactive graph visualization using HTML5 Canvas.

Generates a self-contained HTML file with force-directed layout,
filtering, search, and node inspection. No WebGL required.

Usage:
    nexus visualize --db graph.db
    nexus visualize --db graph.db --max-nodes 500
"""

from __future__ import annotations

import json
import logging
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

_NODE_COLORS = {
    "file": "#4A90D9", "section": "#7B68EE", "function": "#2ECC71",
    "method": "#27AE60", "class": "#E74C3C", "module": "#F39C12",
    "equation": "#9B59B6", "term": "#1ABC9C", "attribute": "#95A5A6",
    "data": "#95A5A6", "exception": "#E74C3C", "type": "#3498DB",
    "external": "#BDC3C7", "unresolved": "#555", "unknown": "#555",
}

_EDGE_COLORS = {
    "calls": "#2ECC71", "imports": "#F39C12", "inherits": "#E74C3C",
    "contains": "#555", "documents": "#4A90D9", "references": "#7B68EE",
    "implements": "#9B59B6", "type_uses": "#1ABC9C", "equation_ref": "#9B59B6",
    "cites": "#E67E22",
}

# The entire visualizer is a single self-contained HTML file.
# Force layout runs in JS (simple velocity Verlet integration).
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Nexus — {node_count} nodes, {edge_count} edges</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:system-ui,sans-serif;background:#1a1a2e;color:#eee;overflow:hidden}}
canvas{{display:block}}
#ui{{position:fixed;top:0;left:0;width:230px;height:100vh;background:rgba(22,33,62,0.95);
  border-right:1px solid #333;padding:12px;overflow-y:auto;font-size:12px}}
#ui h3{{color:#4A90D9;font-size:13px;margin:8px 0 6px}}
#ui label{{display:block;margin:2px 0;cursor:pointer}}
#ui label:hover{{color:#4A90D9}}
#ui input[type=text]{{width:100%;padding:5px 8px;background:#0f1a33;border:1px solid #444;
  color:#eee;border-radius:4px;font-size:12px;margin:4px 0}}
#ui button{{padding:4px 10px;margin:2px;background:#0f1a33;border:1px solid #444;
  color:#eee;border-radius:4px;cursor:pointer;font-size:11px}}
#ui button:hover{{background:#4A90D9}}
#ui hr{{border:none;border-top:1px solid #333;margin:8px 0}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:middle}}
#details{{position:fixed;top:10px;right:10px;width:270px;max-height:80vh;overflow-y:auto;
  background:rgba(22,33,62,0.95);border:1px solid #333;border-radius:8px;padding:12px;
  font-size:12px;display:none}}
#details h4{{color:#4A90D9;word-break:break-all;margin-bottom:6px}}
#details .f{{margin:3px 0}}
#details .f b{{color:#888}}
#details h5{{color:#2ECC71;margin:8px 0 3px;font-size:12px}}
#details .n{{color:#aaa;font-size:11px;margin:1px 0}}
#stats{{position:fixed;bottom:8px;left:240px;color:#666;font-size:11px}}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="ui">
  <h3>Nexus Graph Explorer</h3>
  <input type="text" id="search" placeholder="Search nodes..." oninput="S(this.value)">
  <hr>
  <h3>Node Types</h3><div id="nf"></div>
  <hr>
  <h3>Edge Types</h3><div id="ef"></div>
  <hr>
  <h3>Display</h3>
  <label style="display:flex;align-items:center;gap:6px">Node size
    <input type="range" min="0.5" max="5" step="0.25" value="1" style="flex:1" oninput="setNodeScale(this.value)">
    <span id="szLabel" style="width:28px">1x</span></label>
  <label style="display:flex;align-items:center;gap:6px">Label size
    <input type="range" min="0" max="3" step="0.25" value="1" style="flex:1" oninput="setLabelScale(this.value)">
    <span id="lsLabel" style="width:28px">1x</span></label>
  <label style="display:flex;align-items:center;gap:6px">Edge opacity
    <input type="range" min="0" max="1" step="0.05" value="0.3" style="flex:1" oninput="setEdgeOpacity(this.value)">
    <span id="eoLabel" style="width:28px">0.3</span></label>
  <label style="display:flex;align-items:center;gap:6px">Edge width
    <input type="range" min="0.2" max="4" step="0.2" value="0.5" style="flex:1" oninput="setEdgeWidth(this.value)">
    <span id="ewLabel" style="width:28px">0.5</span></label>
  <label style="display:flex;align-items:center;gap:6px">Clustering
    <input type="range" min="0" max="0.05" step="0.002" value="0.005" style="flex:1" oninput="setClustering(this.value)">
    <span id="clLabel" style="width:36px">0.005</span></label>
  <label style="margin-top:4px"><input type="checkbox" checked onchange="toggleHighlight(this.checked)"> Highlight neighbors on click</label>
  <hr>
  <button onclick="fit()">Fit All</button>
  <button onclick="toggleLabels()">Labels</button>
  <button onclick="reheat()">Re-layout</button>
</div>
<div id="details"><h4 id="dn"></h4><div id="df"></div><div id="dnb"></div></div>
<div id="stats" id="st"></div>
<script>
// DATA
const D={graph_json};
const NC={node_colors_json};
const EC={edge_colors_json};

// STATE
const nodes=[], edges=[], nodeMap={{}};
let W,H,cx=0,cy=0,zoom=0.3,drag=null,pan=null,showLabels=true,search='',sim=300;
let nodeScale=1,labelScale=1,edgeOpacity=0.3,edgeWidth=0.5,highlightMode=true,attractionStrength=0.005;
let selectedNode=null,neighborSet=new Set();
const hiddenNT=new Set(),hiddenET=new Set();
const ntCount={{}},etCount={{}};

// INIT NODES
D.nodes.forEach((n,i)=>{{
  const t=n.type||'unknown';
  ntCount[t]=(ntCount[t]||0)+1;
  const a=Math.random()*Math.PI*2, r=Math.sqrt(Math.random())*2000;
  const node={{
    id:n.id,name:n.name||n.id,type:t,degree:n.degree||1,docname:n.docname||'',
    x:r*Math.cos(a),y:r*Math.sin(a),vx:0,vy:0,
    sz:Math.max(8,Math.min(40,Math.sqrt(n.degree||1)*4)),
    col:NC[t]||'#888'
  }};
  nodes.push(node);
  nodeMap[n.id]=node;
}});

// INIT EDGES
D.edges.forEach(e=>{{
  const t=e.type||'unknown';
  etCount[t]=(etCount[t]||0)+1;
  const s=nodeMap[e.source],tg=nodeMap[e.target];
  if(s&&tg) edges.push({{source:s,target:tg,type:t,col:EC[t]||'#333'}});
}});

// CANVAS
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
function resize(){{W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight}}
window.onresize=resize; resize();

// FORCE LAYOUT
function tick(){{
  if(sim<=0) return;
  sim--;
  const alpha=0.5*(sim/300);
  const vis=nodes.filter(n=>!hiddenNT.has(n.type));
  const N=vis.length;
  // Repulsion — stronger, with minimum distance
  for(let i=0;i<N;i++){{
    for(let j=i+1;j<N;j++){{
      let dx=vis[j].x-vis[i].x, dy=vis[j].y-vis[i].y;
      let d2=dx*dx+dy*dy;
      if(d2<1) {{ dx=Math.random()-0.5; dy=Math.random()-0.5; d2=1; }}
      let f=alpha*5000/d2;
      vis[i].vx-=dx*f; vis[i].vy-=dy*f;
      vis[j].vx+=dx*f; vis[j].vy+=dy*f;
    }}
  }}
  // Attraction along edges — very weak (high edge density)
  edges.forEach(e=>{{
    if(hiddenNT.has(e.source.type)||hiddenNT.has(e.target.type)) return;
    if(hiddenET.has(e.type)) return;
    let dx=e.target.x-e.source.x, dy=e.target.y-e.source.y;
    let d=Math.sqrt(dx*dx+dy*dy)+1;
    let f=alpha*(d-100)/d*attractionStrength;
    e.source.vx+=dx*f; e.source.vy+=dy*f;
    e.target.vx-=dx*f; e.target.vy-=dy*f;
  }});
  // Gentle center gravity
  vis.forEach(n=>{{
    n.vx-=n.x*alpha*0.002;
    n.vy-=n.y*alpha*0.002;
    n.x+=n.vx; n.y+=n.vy;
    n.vx*=0.6; n.vy*=0.6;
  }});
}}

// RENDER
function draw(){{
  tick();
  ctx.clearRect(0,0,W,H);
  ctx.save();
  ctx.translate(W/2+cx,H/2+cy);
  ctx.scale(zoom,zoom);

  // Edges
  ctx.lineWidth=edgeWidth/zoom;
  edges.forEach(e=>{{
    if(hiddenNT.has(e.source.type)||hiddenNT.has(e.target.type)) return;
    if(hiddenET.has(e.type)) return;
    const hl=selectedNode&&highlightMode;
    const isNeighborEdge=hl&&(e.source===selectedNode||e.target===selectedNode);
    ctx.strokeStyle=isNeighborEdge?'#fff':e.col;
    ctx.globalAlpha=hl?(isNeighborEdge?0.9:0.04):edgeOpacity;
    ctx.lineWidth=(isNeighborEdge?2:edgeWidth)/zoom;
    ctx.beginPath();
    ctx.moveTo(e.source.x,e.source.y);
    ctx.lineTo(e.target.x,e.target.y);
    ctx.stroke();
  }});
  ctx.globalAlpha=1;
  ctx.lineWidth=edgeWidth/zoom;

  // Nodes
  const labelMinPx=6;
  nodes.forEach(n=>{{
    if(hiddenNT.has(n.type)) return;
    const dim=search&&!n.name.toLowerCase().includes(search);
    const hl=selectedNode&&highlightMode;
    const isNeighbor=hl&&(n===selectedNode||neighborSet.has(n));
    const faded=hl&&!isNeighbor;
    const r=n.sz*nodeScale;
    const rScreen=r*zoom;
    ctx.beginPath();
    ctx.arc(n.x,n.y,r,0,Math.PI*2);
    ctx.fillStyle=(dim||faded)?'#2a2a3e':n.col;
    ctx.globalAlpha=(dim||faded)?0.15:1;
    ctx.fill();
    // Selected node border
    if(n===selectedNode){{
      ctx.strokeStyle='#fff';
      ctx.lineWidth=3/zoom;
      ctx.stroke();
    }}
    if(showLabels&&!dim&&!faded&&rScreen>labelMinPx*labelScale){{
      const fontSize=Math.max(10,r*0.7)*labelScale;
      ctx.fillStyle='#ccc';
      ctx.font=`${{fontSize}}px system-ui`;
      ctx.textAlign='center';
      const label=n.name.split('.').pop();
      ctx.fillText(label,n.x,n.y+r+fontSize+2);
    }}
    ctx.globalAlpha=1;
  }});

  ctx.restore();

  // Stats
  const vis=nodes.filter(n=>!hiddenNT.has(n.type)).length;
  document.getElementById('stats').textContent=`${{vis}}/${{nodes.length}} nodes shown`;

  requestAnimationFrame(draw);
}}
requestAnimationFrame(draw);

// INTERACTION
canvas.onwheel=e=>{{
  e.preventDefault();
  const f=e.deltaY>0?0.9:1.1;
  zoom*=f;
  zoom=Math.max(0.05,Math.min(10,zoom));
}};
canvas.onmousedown=e=>{{
  const mx=(e.clientX-W/2-cx)/zoom, my=(e.clientY-H/2-cy)/zoom;
  let hit=null, best=Infinity;
  nodes.forEach(n=>{{
    if(hiddenNT.has(n.type)) return;
    const dx=n.x-mx,dy=n.y-my,d2=dx*dx+dy*dy;
    const r=n.sz*nodeScale+6/zoom;
    if(d2<r*r&&d2<best){{ best=d2; hit=n; }}
  }});
  if(hit){{
    drag=hit;
    selectedNode=hit;
    neighborSet.clear();
    edges.forEach(e=>{{
      if(e.source===hit) neighborSet.add(e.target);
      if(e.target===hit) neighborSet.add(e.source);
    }});
    showDetails(hit);
  }} else {{
    pan={{x:e.clientX-cx,y:e.clientY-cy}};
    selectedNode=null; neighborSet.clear();
    hideDetails();
  }}
}};
canvas.onmousemove=e=>{{
  if(drag){{ drag.x=(e.clientX-W/2-cx)/zoom; drag.y=(e.clientY-H/2-cy)/zoom; }}
  if(pan){{ cx=e.clientX-pan.x; cy=e.clientY-pan.y; }}
}};
canvas.onmouseup=()=>{{ drag=null; pan=null; }};

// DETAILS PANEL
function showDetails(n){{
  document.getElementById('details').style.display='block';
  document.getElementById('dn').textContent=n.name;
  let h=`<div class="f"><b>Type:</b> ${{n.type}}</div>`;
  h+=`<div class="f"><b>ID:</b> <code style="font-size:10px">${{n.id}}</code></div>`;
  h+=`<div class="f"><b>Degree:</b> ${{n.degree}}</div>`;
  if(n.docname) h+=`<div class="f"><b>Doc:</b> ${{n.docname}}</div>`;
  document.getElementById('df').innerHTML=h;

  // Neighbors
  const out={{}},inc={{}};
  edges.forEach(e=>{{
    if(e.source===n){{ (out[e.type]=out[e.type]||[]).push(e.target.name); }}
    if(e.target===n){{ (inc[e.type]=inc[e.type]||[]).push(e.source.name); }}
  }});
  let nb='';
  if(Object.keys(out).length){{
    nb+='<h5>Outgoing</h5>';
    for(const[t,ns]of Object.entries(out))
      nb+=`<div class="n"><b>${{t}} (${{ns.length}}):</b> ${{ns.slice(0,5).join(', ')}}${{ns.length>5?' +${{ns.length-5}}':''}}</div>`;
  }}
  if(Object.keys(inc).length){{
    nb+='<h5>Incoming</h5>';
    for(const[t,ns]of Object.entries(inc))
      nb+=`<div class="n"><b>${{t}} (${{ns.length}}):</b> ${{ns.slice(0,5).join(', ')}}${{ns.length>5?' +${{ns.length-5}}':''}}</div>`;
  }}
  document.getElementById('dnb').innerHTML=nb;
}}
function hideDetails(){{ document.getElementById('details').style.display='none'; }}

// FILTERS
function buildFilters(){{
  let h='';
  for(const[t,c]of Object.entries(ntCount).sort((a,b)=>b[1]-a[1]))
    h+=`<label><input type=checkbox checked onchange="TN('${{t}}')"><span class=dot style="background:${{NC[t]||'#888'}}"></span>${{t}} (${{c}})</label>`;
  document.getElementById('nf').innerHTML=h;
  h='';
  for(const[t,c]of Object.entries(etCount).sort((a,b)=>b[1]-a[1]))
    h+=`<label><input type=checkbox checked onchange="TE('${{t}}')"><span class=dot style="background:${{EC[t]||'#444'}}"></span>${{t}} (${{c}})</label>`;
  document.getElementById('ef').innerHTML=h;
}}
buildFilters();

function TN(t){{ if(hiddenNT.has(t)) hiddenNT.delete(t); else hiddenNT.add(t); }}
function TE(t){{ if(hiddenET.has(t)) hiddenET.delete(t); else hiddenET.add(t); }}
function S(v){{ search=v.toLowerCase(); }}
function fit(){{
  // Compute bounding box of visible nodes and zoom to fit
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity,count=0;
  nodes.forEach(n=>{{
    if(hiddenNT.has(n.type)) return;
    if(n.x<minX) minX=n.x; if(n.x>maxX) maxX=n.x;
    if(n.y<minY) minY=n.y; if(n.y>maxY) maxY=n.y;
    count++;
  }});
  if(!count) return;
  const gw=maxX-minX+100, gh=maxY-minY+100;
  const cw=W-240, ch=H; // canvas area minus sidebar
  zoom=Math.min(cw/gw, ch/gh)*0.9;
  zoom=Math.max(0.02,Math.min(5,zoom));
  cx=-(minX+maxX)/2*zoom+120; // offset for sidebar
  cy=-(minY+maxY)/2*zoom;
}}
function toggleLabels(){{ showLabels=!showLabels; }}
function reheat(){{ sim=300; }}
function setNodeScale(v){{ nodeScale=parseFloat(v); document.getElementById('szLabel').textContent=v+'x'; }}
function setLabelScale(v){{ labelScale=parseFloat(v); document.getElementById('lsLabel').textContent=v+'x'; }}
function setEdgeOpacity(v){{ edgeOpacity=parseFloat(v); document.getElementById('eoLabel').textContent=v; }}
function setEdgeWidth(v){{ edgeWidth=parseFloat(v); document.getElementById('ewLabel').textContent=v; }}
function toggleHighlight(on){{ highlightMode=on; if(!on){{ selectedNode=null; neighborSet.clear(); }} }}
function setClustering(v){{ attractionStrength=parseFloat(v); document.getElementById('clLabel').textContent=v; sim=Math.max(sim,100); }}
</script>
</body>
</html>"""


def generate_html(
    db_path: Path,
    output: Path | None = None,
    max_nodes: int = 500,
) -> Path:
    """Generate a self-contained HTML visualization file."""
    from sphinxcontrib.nexus.export import load_sqlite

    kg = load_sqlite(db_path)
    g = kg.nxgraph

    node_degrees = sorted(g.degree(), key=lambda x: x[1], reverse=True)
    included: set[str] = set()
    nodes_json = []

    for node_id, degree in node_degrees:
        if len(included) >= max_nodes:
            break
        attrs = g.nodes[node_id]
        included.add(node_id)
        nodes_json.append({
            "id": node_id,
            "name": attrs.get("name", ""),
            "type": attrs.get("type", "unknown"),
            "degree": degree,
            "docname": attrs.get("docname", ""),
        })

    edges_json = []
    for src, tgt, data in g.edges(data=True):
        if src in included and tgt in included:
            edges_json.append({
                "source": src,
                "target": tgt,
                "type": data.get("type", "unknown"),
            })

    graph_data = {"nodes": nodes_json, "edges": edges_json}

    html = _HTML_TEMPLATE.format(
        node_count=len(nodes_json),
        edge_count=len(edges_json),
        graph_json=json.dumps(graph_data),
        node_colors_json=json.dumps(_NODE_COLORS),
        edge_colors_json=json.dumps(_EDGE_COLORS),
    )

    if output is None:
        output = db_path.parent / "graph.html"

    output.write_text(html, encoding="utf-8")
    logger.info("Visualization written to %s", output)
    return output


def serve_visualization(
    db_path: Path,
    max_nodes: int = 500,
    **kwargs,
) -> None:
    """Generate HTML and open in browser."""
    output = generate_html(db_path, max_nodes=max_nodes)
    print(f"Graph visualization: {output}")
    print(f"  {max_nodes} nodes (top by degree)")
    webbrowser.open(f"file://{output.resolve()}")
