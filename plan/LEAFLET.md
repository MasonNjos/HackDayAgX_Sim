# Satellite map view (Leaflet)

## Context
Companion to [PLAN.md](PLAN.md) and [TEAM_SPLIT.md](TEAM_SPLIT.md). Stretch polish for the demo: show the farm on real satellite imagery instead of the plain SVG grid, so the plan looks like a real farm to judges ("would a farm operator trust this plan?"). It is optional and must never break the demo: the existing SVG map stays and is the default fallback. Do this only after the demo is rehearsed and B's priority-change and 2-opt items are done.

Facts from the code:
- The scenario is in km (`scenarios/farm.json`, `"units": "...; km; ..."`), so 1 grid unit = 1 km. The grid spans about 8 × 6 km.
- `dashboard/src/components/FarmMap.tsx` (102 lines, SVG) takes `{ state, selectedJob, onSelectJob, onToggleEdge }` and is used once in `App.tsx:121`.
- `State` (`dashboard/src/types.ts`) already has everything needed: `nodes` (x, y), `edges` (a, b, label, blocked), `fields` (x, y, area, name), `depots` (node), `tractors` (x, y, `route: [x, y][]`), `jobs` (status, remaining). The server needs no changes.
- `TRACTOR_COLORS` and `fieldJob()` live in `FarmMap.tsx` and are reused.

## Design
- New component `SatMap.tsx` with the **same props** as `FarmMap`, so `App.tsx` swaps between them with a "Map / Satellite" toggle (default: Map, remembered in `localStorage` inside try/catch).
- Grid to lat/lon: `toLatLng(x, y) = [LAT0 + y / 110.574, LON0 + x / (111.320 * cos(LAT0))]` (km to degrees). The origin `LAT0, LON0` is one constant in `geo.ts`: pick a farm region with fields and a river (Person A chooses).
- Plain Leaflet (`leaflet@1.9.4` + `@types/leaflet`), no react-leaflet: create the map once in `useEffect`, keep layer groups in refs, and redraw the layers from `state` on each poll.
- Tiles: Esri World Imagery (no key), attribution required. If tiles fail to load, the toggle back to Map is the fallback.
- Layers: roads as polylines (red dashed when blocked, click calls `onToggleEdge`), fields as squares sized from `area` in hectares (side = sqrt(area × 10 000) m) and coloured by `fieldJob()` status (click calls `onSelectJob`), a depot marker, tractor circle markers in `TRACTOR_COLORS` with planned `route` polylines, and tooltips matching the SVG `<title>` text.

## Person A: Leaflet setup and map shell (owns `dashboard/`)
1. `cd dashboard && npm install leaflet@1.9.4 && npm install -D @types/leaflet`; commit `package.json` and `package-lock.json` in a small commit of their own. Then `docker compose up --build` (the rebuild is needed after a `package.json` change).
2. `dashboard/src/geo.ts`: `LAT0`, `LON0`, `toLatLng(x, y)`, `boundsOf(nodes)`.
3. `dashboard/src/components/SatMap.tsx` shell: map div, `import "leaflet/dist/leaflet.css"`, Esri tile layer plus attribution, `fitBounds` on first render, `map.remove()` on unmount, and empty layer groups for roads, fields, depots and tractors.
4. `App.tsx`: the Map / Satellite toggle next to the map; `styles.css`: map height matches the SVG map and the layout still works at phone width.
5. Export `TRACTOR_COLORS` and `fieldJob` from `FarmMap.tsx` so B can use them (`fieldJob` is not exported yet).

## Person B: drawing the world on the map (`SatMap.tsx` layers, after A's shell is on main)
1. Roads: polylines from node to node, labels as permanent tooltips, blocked style, click calls `onToggleEdge(edge)`.
2. Fields: squares around `toLatLng(f.x, f.y)` coloured by status (pending, assigned, in_progress, done, unassignable; same colours as `styles.css`), a highlight when the field holds `selectedJob`, a tooltip listing the jobs, click calls `onSelectJob`.
3. Depot marker, tractor markers and route polylines in tractor colours; move existing markers with `setLatLng` instead of recreating them, so the map does not flicker on each poll.
4. Check against `FarmMap.tsx` that every interaction (block or reopen a road, "why?" on a field) works the same in both views.

## Sync and order
A's steps 1–3 and 5 first (about 45 min), merge to main; then B's layers (about 1–1.5 h) while A does step 4. Total about 2–2.5 h for two people.

## Verification
- `docker compose up --build`, open http://localhost:5173, switch to Satellite: tiles load and the farm sits on the chosen location.
- Press ▶ Start: tractors move and routes update on each poll without flicker. Click the bridge: it shows as blocked and the coordinator replans (same as in the SVG view). Click a field: the "why?" panel opens.
- Switch back to Map: the SVG view still works. Test with the network off: the Map view still works.
- `docker compose run --rm app pytest` still passes (no backend change). `npm run build` passes `tsc` inside the image (`docker build -t fleet .`).
- Demo rehearsal: decide beforehand whether to present in Satellite view; keep Map ready if the venue Wi-Fi is bad.
