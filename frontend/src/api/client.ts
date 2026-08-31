// API 客户端 —— 封装 fetch，统一错误处理（后端 400/403/404 的中文 detail 直接抛给界面）
import type {
  AdvanceApplication,
  RequestDetail,
  RequestSummary,
} from "./types";

// 作用：统一请求封装：JSON 化、错误提取 detail、中文 message
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let detail = `请求失败（HTTP ${resp.status}）`;
    try {
      const body = await resp.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* 非 JSON 错误体，用默认文案 */
    }
    throw new Error(detail);
  }
  return resp.json() as Promise<T>;
}

function json(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** 提交报销（multipart：发票文件 + 表单） */
export function submitReimburse(form: FormData): Promise<RequestDetail> {
  return request<RequestDetail>("/api/reimburse", { method: "POST", body: form });
}

/** 审批决策 */
export function decide(
  requestId: string,
  payload: { action: string; comment: string; actor: string },
): Promise<RequestDetail> {
  return request<RequestDetail>(`/api/requests/${requestId}/decide`, json("POST", payload));
}

/** 出纳打款（审批通过后确认打款） */
export function pay(
  requestId: string,
  payload: { comment: string; actor: string },
): Promise<RequestDetail> {
  return request<RequestDetail>(`/api/requests/${requestId}/pay`, json("POST", payload));
}

/** 查询报销单详情 */
export function getRequest(requestId: string): Promise<RequestDetail> {
  return request<RequestDetail>(`/api/requests/${requestId}`);
}

/** 报销单列表（可按状态过滤） */
export function listRequests(status?: string): Promise<RequestSummary[]> {
  const q = status ? `?status=${status}` : "";
  return request<RequestSummary[]>(`/api/requests${q}`);
}

/** 创建事前申请 */
export function createAdvance(body: {
  employee_id: string;
  direction: string;
  start_date: string;
  end_date: string;
  estimated_amount: number;
  purpose: string;
}): Promise<AdvanceApplication> {
  return request<AdvanceApplication>("/api/advance", json("POST", body));
}

/** 事前申请列表 */
export function listAdvances(status?: string): Promise<AdvanceApplication[]> {
  const q = status ? `?status=${status}` : "";
  return request<AdvanceApplication[]>(`/api/advances${q}`);
}
