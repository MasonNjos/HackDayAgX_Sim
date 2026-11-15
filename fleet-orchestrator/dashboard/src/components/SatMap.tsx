import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { boundsOf, toLatLng } from "../geo";
import { fieldJob, TRACTOR_COLORS } from "./FarmMap";
import type { Edge, State, Tractor } from "../types";

interface Props {
  state: State;
  selectedJob: string | null;
  onSelectJob: (id: string) => void;
  onToggleEdge: (edge: Edge) => void;
}

/** Layer groups the map draws into (see plan/LEAFLET.md). */
export interface SatLayers {
  roads: L.LayerGroup;
  fields: L.LayerGroup;
  depots: L.LayerGroup;
  tractors: L.LayerGroup;
}

/** Tractor marker and route kept across polls, so they move instead of being recreated. */
interface TractorLayer {
  marker: L.Marker;
  route: L.Polyline;
  html: string;
}

const TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const ATTRIBUTION = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";

const TRACTOR_PX = 22;    // tractor marker diameter
const TRACTOR_SPREAD = 16; // px between tractors standing on the same node
const TRACTOR_LIFT = 20;   // px the marker sits above its position

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const lines = (s: string) => esc(s).replace(/\n/g, "<br>");

function tractorTitle(t: Tractor) {
  return `${t.id} ${t.implement} · ${t.status} · fuel ${t.fuel}/${t.fuel_capacity} L`;
}

function tractorHtml(t: Tractor, color: string) {
  const frac = Math.max(0, Math.min(1, t.fuel / t.fuel_capacity));
  return `<div class="sat-tractor ${t.status}${frac < 0.2 ? " low-fuel" : ""}" style="background:${color}">`
    + `${esc(t.id)}${t.status === "broken" ? '<span class="broken-x">⚠</span>' : ""}</div>`;
}

/** Satellite view of the farm. Same props as FarmMap, so App can swap between them. */
export function SatMap({ state, selectedJob, onSelectJob, onToggleEdge }: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layersRef = useRef<SatLayers | null>(null);
  const tractorsRef = useRef(new Map<string, TractorLayer>());

  // Create the map once; remove it on unmount.
  useEffect(() => {
    if (!divRef.current) return;
    const map = L.map(divRef.current, { zoomSnap: 0.25 });
    L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 18 }).addTo(map);
    map.fitBounds(boundsOf(state.nodes));
    layersRef.current = {
      roads: L.layerGroup().addTo(map),
      fields: L.layerGroup().addTo(map),
      depots: L.layerGroup().addTo(map),
      tractors: L.layerGroup().addTo(map),
    };
    mapRef.current = map;
    const tractors = tractorsRef.current;
    return () => { map.remove(); mapRef.current = null; layersRef.current = null; tractors.clear(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fit only on first render
  }, []);

  // Roads, fields and depots: few shapes, so redraw them all on each change.
  useEffect(() => {
    const layers = layersRef.current;
    if (!layers) return;
    const node = Object.fromEntries(state.nodes.map((n) => [n.id, n]));

    layers.roads.clearLayers();
    for (const e of state.edges) {
      const a = node[e.a], b = node[e.b];
      if (!a || !b) continue;
      const pts = [toLatLng(a.x, a.y), toLatLng(b.x, b.y)];
      const title = `${e.label || e.id} · ${e.length} km — click to ${e.blocked ? "reopen" : "block"}`;
      const line = L.polyline(pts, {
        className: `sat-road${e.blocked ? " blocked" : ""}`, weight: 5, lineCap: "round", interactive: false,
      });
      // Wide, almost invisible line on top so the road is easy to click.
      const hit = L.polyline(pts, { className: "sat-road-hit", weight: 18, opacity: 0.01 })
        .bindTooltip(esc(title), { sticky: true })
        .on("click", () => onToggleEdge(e));
      layers.roads.addLayer(line).addLayer(hit);
      if (e.label || e.blocked) {
        const mid = toLatLng((a.x + b.x) / 2, (a.y + b.y) / 2);
        const text = `${esc(e.label)}${e.blocked ? ' <span class="sat-road-x">✕</span>' : ""}`;
        layers.roads.addLayer(L.tooltip({ permanent: true, direction: "top", className: "sat-road-label", interactive: false })
          .setLatLng(mid).setContent(text));
      }
    }

    layers.fields.clearLayers();
    for (const f of state.fields) {
      const jobs = state.jobs.filter((j) => j.field === f.id);
      const j = fieldJob(jobs);
      const half = Math.sqrt(f.area * 10_000) / 1000 / 2; // ha -> km, half the side
      const selected = jobs.some((x) => x.id === selectedJob);
      const progress = j?.status === "in_progress" ? ` · ${Math.round((1 - j.remaining) * 100)}% done` : "";
      const title = `${f.name} (${f.area} ha)${progress}\n${jobs.map((x) => `${x.id} ${x.type} P${x.priority} ${x.status}`).join("\n")}`;
      const rect = L.rectangle([toLatLng(f.x - half, f.y - half), toLatLng(f.x + half, f.y + half)], {
        className: `sat-field status-${j?.status ?? "none"}${selected ? " selected" : ""}`,
        weight: selected ? 4 : 1.5,
      }).bindTooltip(lines(title), { sticky: true });
      if (j) rect.on("click", () => onSelectJob(j.id));
      layers.fields.addLayer(rect);
    }

    layers.depots.clearLayers();
    for (const d of state.depots) {
      const n = node[d.node];
      if (!n) continue;
      layers.depots.addLayer(L.marker(toLatLng(n.x, n.y), {
        icon: L.divIcon({ className: "sat-depot", html: "⛽", iconSize: [26, 26], iconAnchor: [13, 13] }),
        keyboard: false,
      }).bindTooltip(esc(`Depot ${d.id} (refuel)`)));
    }
  }, [state, selectedJob, onSelectJob, onToggleEdge]);

  // Tractors and their routes: move the existing layers so the map does not flicker each poll.
  useEffect(() => {
    const layers = layersRef.current;
    if (!layers) return;
    const known = tractorsRef.current;
    const seen = new Set<string>();

    state.tractors.forEach((t, i) => {
      seen.add(t.id);
      const color = TRACTOR_COLORS[i % TRACTOR_COLORS.length];
      const ox = (i - (state.tractors.length - 1) / 2) * TRACTOR_SPREAD;
      const html = tractorHtml(t, color);
      const route = t.route.length > 1 ? t.route.map(([x, y]) => toLatLng(x, y)) : [];
      let layer = known.get(t.id);
      if (!layer) {
        const routeLine = L.polyline(route, { className: "sat-route", color, weight: 3, dashArray: "6 6", interactive: false });
        const marker = L.marker(toLatLng(t.x, t.y), { icon: L.divIcon({ className: "", html }), keyboard: false, zIndexOffset: 1000 })
          .bindTooltip(esc(tractorTitle(t)));
        layers.tractors.addLayer(routeLine).addLayer(marker);
        layer = { marker, route: routeLine, html: "" };
        known.set(t.id, layer);
      } else {
        layer.marker.setLatLng(toLatLng(t.x, t.y));
        layer.marker.setTooltipContent(esc(tractorTitle(t)));
        layer.route.setLatLngs(route).setStyle({ color });
      }
      // Rebuild the icon only when it looks different (status, fuel, colour or offset).
      const key = `${html}|${ox}`;
      if (layer.html !== key) {
        const r = TRACTOR_PX / 2;
        layer.marker.setIcon(L.divIcon({
          className: "", html, iconSize: [TRACTOR_PX, TRACTOR_PX], iconAnchor: [r - ox, r + TRACTOR_LIFT],
        }));
        layer.html = key;
      }
    });

    for (const [id, layer] of known) {
      if (seen.has(id)) continue;
      layers.tractors.removeLayer(layer.marker).removeLayer(layer.route);
      known.delete(id);
    }
  }, [state]);

  return <div ref={divRef} className="sat-map" role="region" aria-label="Farm satellite map" />;
}
