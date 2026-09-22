import axios, { type AxiosRequestConfig } from 'axios';
import type { ApiResponse } from '@/types/api';

const apiClient = axios.create({
  baseURL: '',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
  paramsSerializer: {
    indexes: null,
  },
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    // 后端统一错误体是 BaseResponse { message }（FastAPI 原生/Hub 直返为 { detail }）。
    // 透出到 error.message，否则调用点只能看到 axios 默认的
    // "Request failed with status code N"，丢失后端给的真实原因。
    const data = error.response?.data;
    const serverMessage =
      (typeof data?.message === 'string' && data.message) ||
      (typeof data?.detail === 'string' && data.detail) ||
      '';
    if (serverMessage) {
      error.message = serverMessage;
    }
    if (error.response?.status === 401) {
      const loginPath = '/vllm_manager/frontend/login';
      const onLoginPage = window.location.pathname.startsWith(loginPath);
      if (!onLoginPage) {
        const currentTarget = `${window.location.pathname.replace('/vllm_manager/frontend', '')}${window.location.search}${window.location.hash}`;
        window.location.href = `${loginPath}?redirect=${encodeURIComponent(currentTarget)}`;
      }
    }
    return Promise.reject(error);
  },
);

export async function apiGet<T>(url: string, params?: Record<string, unknown>): Promise<ApiResponse<T>> {
  const response = await apiClient.get<ApiResponse<T>>(url, { params });
  return response.data;
}

export async function apiPost<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<ApiResponse<T>> {
  const response = await apiClient.post<ApiResponse<T>>(url, data, config);
  return response.data;
}

export async function apiDownload(url: string, params?: Record<string, unknown>): Promise<Blob> {
  const response = await apiClient.get(url, {
    params,
    responseType: 'blob',
  });
  return response.data;
}

export async function apiPut<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<ApiResponse<T>> {
  const response = await apiClient.put<ApiResponse<T>>(url, data, config);
  return response.data;
}

export async function apiPatch<T>(url: string, data?: unknown): Promise<ApiResponse<T>> {
  const response = await apiClient.patch<ApiResponse<T>>(url, data);
  return response.data;
}

export async function apiDelete<T>(url: string, data?: unknown): Promise<ApiResponse<T>> {
  const response = await apiClient.delete<ApiResponse<T>>(url, data ? { data } : undefined);
  return response.data;
}

export default apiClient;
