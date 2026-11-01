/**
 * Static glass for interactive UI; opt-in bounded SVG backdrop refraction.
 * Sources and licenses: third_party/liquid_glass/PROVENANCE.txt.
 * Text and controls remain normal DOM; only the sampled background is warped.
 */
import { useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties, HTMLAttributes } from 'react';
import './glass-surface.css';

export interface GlassSurfaceProps extends HTMLAttributes<HTMLDivElement> {
  radius?: number;
  intensity?: number;
  /** Keep live editors out of Qt WebEngine's SVG backdrop compositing path. */
  refraction?: boolean;
}

type Renderer = 'static' | 'svg' | 'frosted' | 'solid';
type DisplacementMap = { width: number; height: number; url: string; key: string };
type Material = { renderer: Renderer; map: DisplacementMap | null };

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, value));

function smoothStep(start: number, end: number, value: number) {
  const t = clamp((value - start) / (end - start), 0, 1);
  return t * t * (3 - 2 * t);
}

/** Generate at most 160,000 pixels once per measured size/radius change. */
function createDisplacementMap(width: number, height: number, radius: number): DisplacementMap | null {
  const scale = Math.min(1, 640 / width, 640 / height, Math.sqrt(160_000 / (width * height)));
  const mapWidth = Math.max(2, Math.floor(width * scale));
  const mapHeight = Math.max(2, Math.floor(height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = mapWidth;
  canvas.height = mapHeight;
  const context = canvas.getContext('2d');
  if (!context) return null;
  try {
    const pixels = context.createImageData(mapWidth, mapHeight);
    const halfWidth = mapWidth / 2;
    const halfHeight = mapHeight / 2;
    const corner = clamp(radius * scale, 0, Math.min(halfWidth, halfHeight));
    const band = Math.max(2, Math.min(36, Math.min(width, height) * .22) * scale);
    for (let y = 0; y < mapHeight; y += 1) {
      for (let x = 0; x < mapWidth; x += 1) {
        const px = x + .5 - halfWidth;
        const py = y + .5 - halfHeight;
        const qx = Math.abs(px) - halfWidth + corner;
        const qy = Math.abs(py) - halfHeight + corner;
        // Rounded-rectangle SDF, following Shu Ding's public shader example.
        const distance = Math.min(Math.max(qx, qy), 0) + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - corner;
        let nx = 0;
        let ny = 0;
        if (qx > 0 && qy > 0) {
          const length = Math.hypot(qx, qy);
          nx = Math.sign(px) * qx / length;
          ny = Math.sign(py) * qy / length;
        } else if (qx > qy) nx = Math.sign(px);
        else ny = Math.sign(py);
        const edge = distance <= 0 ? 1 - smoothStep(0, band, -distance) : 0;
        const bend = edge * edge;
        const index = (y * mapWidth + x) * 4;
        pixels.data[index] = Math.round(127.5 + nx * bend * 112);
        pixels.data[index + 1] = Math.round(127.5 + ny * bend * 112);
        pixels.data[index + 2] = 128;
        pixels.data[index + 3] = 255;
      }
    }
    context.putImageData(pixels, 0, 0);
    return { width, height, url: canvas.toDataURL('image/png'), key: `${width}:${height}:${radius}` };
  } finally {
    // The data URL owns the bounded result; release the temporary pixel buffer.
    canvas.width = 0;
    canvas.height = 0;
  }
}

function supportsSvgBackdrop() {
  return typeof CSS !== 'undefined' && typeof SVGFEDisplacementMapElement !== 'undefined'
    && (CSS.supports('backdrop-filter', 'url("#glass-support-probe") blur(1px)')
      || CSS.supports('-webkit-backdrop-filter', 'url("#glass-support-probe") blur(1px)'));
}

export function GlassSurface({ children, className = '', style, radius = 24, intensity = 22, refraction = false,
  ...attributes }: GlassSurfaceProps) {
  const root = useRef<HTMLDivElement>(null);
  const id = `aura-glass-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const corner = Number.isFinite(radius) ? clamp(radius, 0, 160) : 24;
  const strength = Number.isFinite(intensity) ? clamp(intensity, 0, 40) : 22;
  const chromaticOffset = Math.min(.6, strength * .04);
  const [material, setMaterial] = useState<Material>({ renderer: 'static', map: null });

  useEffect(() => {
    const element = root.current;
    if (!element) return;
    let disposed = false;
    let intersecting = true;
    let timer: number | undefined;
    let cached: DisplacementMap | null = null;
    let observingSize = false;
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const reducedTransparency = window.matchMedia('(prefers-reduced-transparency: reduce)');
    const forcedColors = window.matchMedia('(forced-colors: active)');
    const increasedContrast = window.matchMedia('(prefers-contrast: more)');
    const mediaQueries = [reducedMotion, reducedTransparency, forcedColors, increasedContrast];
    const supported = refraction && supportsSvgBackdrop();
    const update = (renderer: Renderer, map = cached) => {
      if (disposed) return;
      setMaterial(previous => previous.renderer === renderer && previous.map === map
        ? previous : { renderer, map });
    };
    const policy = (): Renderer => forcedColors.matches || reducedTransparency.matches || increasedContrast.matches
      ? 'solid' : !refraction ? 'static' : reducedMotion.matches || !supported ? 'frosted' : 'svg';
    const cancelScheduled = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = undefined;
    };
    const renderMap = () => {
      timer = undefined;
      if (disposed || document.hidden || !intersecting || policy() !== 'svg') return;
      const bounds = element.getBoundingClientRect();
      const width = Math.round(bounds.width);
      const height = Math.round(bounds.height);
      if (width < 2 || height < 2 || !element.getClientRects().length) {
        update('frosted');
        return;
      }
      const key = `${width}:${height}:${corner}`;
      if (cached?.key !== key) {
        try {
          cached = createDisplacementMap(width, height, corner);
        } catch {
          cached = null;
        }
      }
      update(cached ? 'svg' : 'frosted');
    };
    const scheduleMap = () => {
      if (timer === undefined) timer = window.setTimeout(renderMap, 0);
    };
    const resizeObserver = refraction && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => {
      if (!document.hidden && intersecting && policy() === 'svg') scheduleMap();
    }) : null;
    const sync = () => {
      const next = policy();
      const active = next === 'svg' && !document.hidden && intersecting;
      if (active) {
        if (resizeObserver && !observingSize) {
          resizeObserver.observe(element);
          observingSize = true;
        }
        scheduleMap();
      } else {
        cancelScheduled();
        resizeObserver?.disconnect();
        observingSize = false;
        update(next === 'svg' ? 'frosted' : next);
      }
    };
    const intersectionObserver = refraction && typeof IntersectionObserver !== 'undefined'
      ? new IntersectionObserver(entries => {
        intersecting = entries[0]?.isIntersecting ?? true;
        sync();
      }) : null;
    intersectionObserver?.observe(element);
    if (refraction) document.addEventListener('visibilitychange', sync);
    for (const query of mediaQueries) query.addEventListener('change', sync);
    if (refraction && !resizeObserver) window.addEventListener('resize', sync);
    sync();
    return () => {
      disposed = true;
      cancelScheduled();
      resizeObserver?.disconnect();
      intersectionObserver?.disconnect();
      if (refraction) document.removeEventListener('visibilitychange', sync);
      for (const query of mediaQueries) query.removeEventListener('change', sync);
      if (refraction && !resizeObserver) window.removeEventListener('resize', sync);
      cached = null;
    };
  }, [corner, refraction]);

  const materialStyle = {
    '--glass-radius': `${corner}px`,
    '--glass-backdrop': material.renderer === 'svg' ? `url("#${id}") blur(1.2px) saturate(1.12)` : undefined,
    ...style,
  } as CSSProperties;
  const map = material.map;
  return <div {...attributes} ref={root} className={`glass-surface ${className}`.trim()}
    style={materialStyle} data-glass-renderer={material.renderer}>
    {children}
    {material.renderer === 'svg' && map && <svg width="0" height="0" aria-hidden="true" focusable="false"
      className="glass-surface-definitions" style={{ position: 'absolute', pointerEvents: 'none' }}>
      <defs><filter id={id} x="0" y="0" width={map.width} height={map.height}
        filterUnits="userSpaceOnUse" colorInterpolationFilters="sRGB">
        <feImage href={map.url} width={map.width} height={map.height} preserveAspectRatio="none" result="map" />
        <feDisplacementMap in="SourceGraphic" in2="map" scale={strength + chromaticOffset}
          xChannelSelector="R" yChannelSelector="G" result="red-shift" />
        <feColorMatrix in="red-shift" type="matrix" values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0" result="red" />
        <feDisplacementMap in="SourceGraphic" in2="map" scale={strength}
          xChannelSelector="R" yChannelSelector="G" result="green-shift" />
        <feColorMatrix in="green-shift" type="matrix" values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0" result="green" />
        <feDisplacementMap in="SourceGraphic" in2="map" scale={Math.max(0, strength - chromaticOffset)}
          xChannelSelector="R" yChannelSelector="G" result="blue-shift" />
        <feColorMatrix in="blue-shift" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0" result="blue" />
        <feBlend in="green" in2="blue" mode="screen" result="green-blue" />
        <feBlend in="red" in2="green-blue" mode="screen" />
      </filter></defs>
    </svg>}
  </div>;
}

export default GlassSurface;
