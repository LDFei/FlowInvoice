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

/** 重试失败的异步任务（failed → pending 重新投递；返回新状态，前端继续轮询） */
export function retrySubmission(requestId: string): Promise<SubmissionStatus> {
  return request<SubmissionStatus>(`/api/submissions/${requestId}/retry`, { method: "POST" });
}

/** 异步提交任务列表（报销端"处理失败的任务"用；employeeId 过滤本人、status 可选） */
export function listSubmissions(params: { employeeId?: string; status?: string } = {}): Promise<SubmissionStatus[]> {
  const qs = new URLSearchParams();
  if (params.employeeId) qs.set("employee_id", params.employeeId);
  if (params.status) qs.set("status", params.status);
  const s = qs.toString();
  return request<SubmissionStatus[]>(`/api/submissions${s ? `?${s}` : ""}`);
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
/** #93 发票原件取用：拼下载/预览 URL（同源相对路径 → Vite 代理到后端；对象存储权威副本） */
export function originalUrl(requestId: string, objectKey: string): string {
  return `/api/requests/${encodeURIComponent(requestId)}/originals/${encodeURIComponent(objectKey)}`;
}

export function getRequest(requestId: string): Promise<RequestDetail> {
  return request<RequestDetail>(`/api/requests/${requestId}`);
}

/** 报销单列表（可按状态/申报人/待办审批人过滤；#70 数据隔离——报销端看我的、审批端看我的待办） */
export function listRequests(
  status?: string,
  filters?: { employee_id?: string; approver_id?: string },
): Promise<RequestSummary[]> {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (filters?.employee_id) params.set("employee_id", filters.employee_id);
  if (filters?.approver_id) params.set("approver_id", filters.approver_id);
  const qs = params.toString();
  return request<RequestSummary[]>(`/api/requests${qs ? `?${qs}` : ""}`);
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
