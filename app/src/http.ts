import axios from "axios";
import i18n from "./i18n";
import { Neato, type QuickRobotInfo } from "./types";
import type { NoGoLine, VacuulaMapData, Zone } from "./components/map/types";

declare module 'axios' {
  export interface AxiosRequestConfig {
    skipLoader?: boolean;
  }
}

/** One field of a plugin's settings form */
export interface PluginConfigField {
  key: string
  label?: string
  type: 'text' | 'number' | 'switch' | 'dropdown' | 'password'
  default?: unknown
  options?: string[]
}

export type PluginConfig = Record<string, string | number | boolean>

/** GET /api/plugins */
export interface PluginInfo {
  id: string
  name: string
  description: string
  version: string
  source: 'builtin' | 'user' | 'package'
  loaded: boolean
  enabled: boolean
  status: 'running' | 'stopped' | 'error'
  error: string | null
  /** problem reported by a running plugin, e.g. cannot reach its server */
  warning: string | null
  config_schema: PluginConfigField[]
  config: PluginConfig
}

/** GET /api/server */
export interface ServerInfo {
  timezone: string
  utc_offset_minutes: number
  version: string
  gen4_port: number
}

// Store for loading state callback - will be set by AppContext
// let loadingStateCallback: ((state: { isLoading: boolean; message?: string }) => void) | null = null;

const instance = axios.create({
  baseURL: `${location.origin}/api`, 
});

export type LoadingState = { isLoading: boolean; message?: string };

let loadingStateCallback: ((state: LoadingState) => void) | null = null;
let pendingRequests = 0;
let hideTimer: ReturnType<typeof setTimeout> | null = null;

const startLoading = () => {
  pendingRequests += 1;
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  if (pendingRequests === 1) {
    loadingStateCallback?.({ isLoading: true, message: i18n.t('common.sendingCommand') });
  }
};

const stopLoading = () => {
  pendingRequests = Math.max(0, pendingRequests - 1);
  if (pendingRequests === 0) {
    // small delay avoids flicker when requests fire back to back
    hideTimer = setTimeout(() => {
      hideTimer = null;
      loadingStateCallback?.({ isLoading: false });
    }, 150);
  }
};


instance.interceptors.request.use((config) => {
  if (!config.skipLoader) startLoading();
  return config;
});

instance.interceptors.response.use(
  (response) => {
    if (!response.config.skipLoader) stopLoading();
    return response;
  },
  (error) => {
    if (!error.config?.skipLoader) stopLoading();
    return Promise.reject(error);
  }
);

export class Http {
  static sendCmd<T>(robotSerial: string, cmd: object) {
    return instance.post<T>(`/robot/${robotSerial}/command`, JSON.stringify(cmd));
  }

  static getPersistentMaps(serial: string) {
    return instance.get<Neato.Gen3.PersistentMap[] | Neato.Gen4.Floorplan[]>(`/robot/${serial}/maps`)
  }

  static getPersistentMap(serial: string, mapId: string) {
    return instance.get<VacuulaMapData>(`/robot/${serial}/maps/${mapId}`)
  }

  static postUseMapAsFloorPlan(serial: string, mapId: string) {
    return instance.post(`/robot/${serial}/maps/${mapId}/use`)
  }

  static postUnuseMapAsFloorPlan(serial: string, mapId: string) {
    return instance.post(`/robot/${serial}/maps/${mapId}/unuse`)
  }

  static updatePersistentMap(serial: string, mapId: string, data: { name?: string, enabled?: boolean }) {
    return instance.post(`/robot/${serial}/maps/${mapId}`, data)
  }

  static postBoundaries(serial: string, mapId: string, data: (NoGoLine | Zone)[]) {
    return instance.post(`/robot/${serial}/maps/${mapId}/boundaries`, data)
  }

  static getBoundaries(serial: string, mapId: string) {
    return instance.get(`/robot/${serial}/maps/${mapId}/boundaries`)
  }

  static promoteFloorplan(serial: string, mapId: string) {
    return instance.post(`/robot/${serial}/maps/${mapId}/promote`)
  }

  static getCleanings(serial: string) {
    return instance.get(`/robot/${serial}/runs`)
  }

  static getCleaning(serial: string, runId: string) {
    return instance.get(`/robot/${serial}/run/${runId}`)
  }

  static demoteFloorplan(serial: string, mapId: string) {
    return instance.delete(`/robot/${serial}/maps/${mapId}`)
  }

  static getRobots() {
    return instance.get<QuickRobotInfo>('/robots');
  }

  /** Server facts the UI needs: the IANA timezone it runs in and its current UTC offset. */
  static getServerInfo() {
    return instance.get<ServerInfo>('/server');
  }

  static getVacuulaCert() {
    return instance.get('/cert');
  }

  static updateRobot(serial: string, data: { name: string }) {
    return instance.post(`/robot/${serial}`, data)
  }

  static getPlugins() {
    return instance.get<PluginInfo[]>('/plugins', { skipLoader: true })
  }

  static updatePlugin(id: string, data: { enabled?: boolean, config?: PluginConfig }) {
    return instance.post<PluginInfo>(`/plugins/${id}`, data)
  }

  static reloadPlugins() {
    return instance.post<PluginInfo[]>('/plugins/reload')
  }

  static setLoadingStateCallback(cb: ((state: LoadingState) => void) | null) {
    loadingStateCallback = cb;
  }
}


