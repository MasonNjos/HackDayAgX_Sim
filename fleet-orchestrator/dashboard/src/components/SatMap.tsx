import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { boundsOf } from "../geo";
import type { Edge, State } from "../types";

interface Props {
  state: State;
  selectedJob: string | null;
  onSelectJob: (id: string) => void;
  onToggleEdge: (edge: Edge) => void;
}

/** Layer groups Person B draws into (see plan/LEAFLET.md). */
export interface SatLayers {
  roads: L.LayerGroup;
  fields: L.LayerGroup;
  depots: L.LayerGroup;
  tractors: L.LayerGroup;
}

const TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const ATTRIBUTION = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";

/** Satellite view of the farm. Same props as FarmMap, so App can swap between them. */
export function SatMap({ state }: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layersRef = useRef<SatLayers | null>(null);

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
    return () => { map.remove(); mapRef.current = null; layersRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fit only on first render
  }, []);

  // Person B: redraw roads, fields, depots and tractors from `state` here.
  useEffect(() => {
    if (!layersRef.current) return;
  }, [state]);

  return <div ref={divRef} className="sat-map" role="region" aria-label="Farm satellite map" />;
}
