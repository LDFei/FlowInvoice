// API 客户端 —— 封装 fetch，统一错误处理（后端 400/403/404 的中文 detail 直接抛给界面）
import type {
  AdvanceApplication,
  RequestDetail,
  RequestSummary,
  SubmissionStatus,
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

/** 提交报销结果：异步模式=受理凭证(202) / 同步模式=完整详情 */
export type SubmitResult = { accepted: true; request_id: string } | RequestDetail;

/** 提交报销（multipart：发票文件 + 表单）
 *  异步模式（FLOWINVOICE_ASYNC=1）后端返回 202 受理凭证 → 前端用 request_id 轮询任务状态；
 *  同步模式返回完整详情（历史行为）。
 */
export async function submitReimburse(form: FormData): Promise<SubmitResult> {
  const resp = await fetch("/api/reimburse", { method: "POST", body: form });
  const body = await resp.json().catch(() => null);
  if (resp.status === 202 && body?.request_id) {
    return { accepted: true, request_id: body.request_id as string };
  }
  if (!resp.ok) {
    let detail = `请求失败（HTTP ${resp.status}）`;
    if (body && typeof body.detail === "string") detail = body.detail;
    throw new Error(detail);
  }
  return body as RequestDetail;
}

/** 查询异步提交任务状态（pending/processing/succeeded/failed，202 受理后轮询用） */
export function getSubmission(requestId: string): Promise<SubmissionStatus> {
  return request<SubmissionStatus>(`/api/submissions/${requestId}`);
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

/** 事前申请列表（可按状态/员工过滤；返回含已占用与剩余额度） */
export function listAdvances(status?: string, employeeId?: string): Promise<AdvanceApplication[]> {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (employeeId) params.set("employee_id", employeeId);
  const qs = params.toString();
  return request<AdvanceApplication[]>(`/api/advances${qs ? `?${qs}` : ""}`);
}
