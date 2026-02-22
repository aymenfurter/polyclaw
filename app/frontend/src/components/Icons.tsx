import type { SVGProps } from 'react'

type P = SVGProps<SVGSVGElement>

function I(props: P) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={18}
      height={18}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    />
  )
}

export function IconMessage(p: P) {
  return <I {...p}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></I>
}

export function IconPlus(p: P) {
  return <I {...p}><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></I>
}

export function IconArrowUp(p: P) {
  return <I {...p}><line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" /></I>
}

export function IconClock(p: P) {
  return <I {...p}><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></I>
}

export function IconZap(p: P) {
  return <I {...p}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></I>
}

export function IconSliders(p: P) {
  return (
    <I {...p}>
      <line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" />
      <line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" />
      <line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" />
      <line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" />
      <line x1="17" y1="16" x2="23" y2="16" />
    </I>
  )
}

export function IconUser(p: P) {
  return <I {...p}><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></I>
}

export function IconChevronDown(p: P) {
  return <I {...p}><polyline points="6 9 12 15 18 9" /></I>
}

export function IconChevronRight(p: P) {
  return <I {...p}><polyline points="9 18 15 12 9 6" /></I>
}

export function IconX(p: P) {
  return <I {...p}><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></I>
}

export function IconSearch(p: P) {
  return <I {...p}><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></I>
}

export function IconPackage(p: P) {
  return (
    <I {...p}>
      <line x1="16.5" y1="9.4" x2="7.55" y2="4.24" />
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
      <line x1="12" y1="22.08" x2="12" y2="12" />
    </I>
  )
}

export function IconServer(p: P) {
  return (
    <I {...p}>
      <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
      <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
      <line x1="6" y1="6" x2="6.01" y2="6" />
      <line x1="6" y1="18" x2="6.01" y2="18" />
    </I>
  )
}

export function IconCalendar(p: P) {
  return (
    <I {...p}>
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
      <line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </I>
  )
}

export function IconBell(p: P) {
  return <I {...p}><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></I>
}

export function IconCloud(p: P) {
  return <I {...p}><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" /></I>
}

export function IconDatabase(p: P) {
  return (
    <I {...p}>
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </I>
  )
}

export function IconFolder(p: P) {
  return <I {...p}><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></I>
}

export function IconFile(p: P) {
  return <I {...p}><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><polyline points="13 2 13 9 20 9" /></I>
}

export function IconTrash(p: P) {
  return <I {...p}><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></I>
}

export function IconRefresh(p: P) {
  return <I {...p}><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></I>
}

export function IconCheck(p: P) {
  return <I {...p}><polyline points="20 6 9 17 4 12" /></I>
}

export function IconWarning(p: P) {
  return (
    <I {...p}>
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
    </I>
  )
}

export function IconExternalLink(p: P) {
  return (
    <I {...p}>
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" /><line x1="10" y1="14" x2="21" y2="3" />
    </I>
  )
}

export function IconUpload(p: P) {
  return (
    <I {...p}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" />
    </I>
  )
}

export function IconPanelLeft(p: P) {
  return <I {...p}><rect x="3" y="3" width="18" height="18" rx="2" /><line x1="9" y1="3" x2="9" y2="21" /></I>
}

export function IconEdit(p: P) {
  return (
    <I {...p}>
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
    </I>
  )
}

export function IconPalette(p: P) {
  return (
    <I {...p}>
      <circle cx="13.5" cy="6.5" r="0.5" fill="currentColor" /><circle cx="17.5" cy="10.5" r="0.5" fill="currentColor" />
      <circle cx="8.5" cy="7.5" r="0.5" fill="currentColor" /><circle cx="6.5" cy="12" r="0.5" fill="currentColor" />
      <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.687-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.563-2.512 5.563-5.563C22 6.312 17.5 2 12 2z" />
    </I>
  )
}

export function IconBrain(p: P) {
  return (
    <I {...p}>
      <path d="M9.5 2a2.5 2.5 0 0 1 2.45 2A2.5 2.5 0 0 1 14.5 2 2.5 2.5 0 0 1 17 4.5c0 .28-.05.55-.13.8A3 3 0 0 1 19 8a3 3 0 0 1-1.1 2.32A3 3 0 0 1 19 13a3 3 0 0 1-2.13 2.87A2.5 2.5 0 0 1 14.5 18a2.5 2.5 0 0 1-2.45-2" />
      <path d="M14.5 2A2.5 2.5 0 0 0 12 4v16a2.5 2.5 0 0 0 2.5 2" />
      <path d="M9.5 2A2.5 2.5 0 0 0 7 4.5c0 .28.05.55.13.8A3 3 0 0 0 5 8a3 3 0 0 0 1.1 2.32A3 3 0 0 0 5 13a3 3 0 0 0 2.13 2.87A2.5 2.5 0 0 0 9.5 18a2.5 2.5 0 0 0 2.45-2" />
      <path d="M9.5 2A2.5 2.5 0 0 1 12 4v16a2.5 2.5 0 0 1-2.5 2" />
    </I>
  )
}

export function IconTerminal(p: P) {
  return <I {...p}><polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" /></I>
}

export function IconShield(p: P) {
  return <I {...p}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></I>
}

export function IconActivity(p: P) {
  return <I {...p}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></I>
}

export function IconFingerprint(p: P) {
  return <I {...p}><path d="M2 12C2 6.5 6.5 2 12 2a10 10 0 0 1 8 4" /><path d="M5 19.5C5.5 18 6 15 6 12c0-3.5 2.5-6 6-6 1 0 2 .2 3 .5" /><path d="M12 12c0 4-1.5 7.5-3.5 10" /><path d="M18 12c0 3-1 5.5-2.5 8" /><path d="M22 12c0 2.5-.5 5-1.5 7" /></I>
}
