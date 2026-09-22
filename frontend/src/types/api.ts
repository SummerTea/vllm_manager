/**
 * 标准 API 响应结构 — 与 FastAPI 后端 BaseResponse 对齐
 */
export interface ApiResponse<T = unknown> {
  code: number;
  message: string;
  data?: T | null;
}

/**
 * 分页元数据
 */
export interface PageMeta {
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

/**
 * 分页数据包装
 */
export interface PageData<T> {
  items: T[];
  meta: PageMeta;
}
