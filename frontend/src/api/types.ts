// API 类型定义 —— 与后端 app/api/schemas.py 一一对应
// 业务：类型即契约，后端 response_model 改了这里同步改（docs/07 Bug 10）

/** 流程状态 */
export type ProcessStatus = "in_review" | "returned" | "approved" | "paid" | "voided";

/** 异步提交任务状态（#53：202 受理后前端轮询用；与后端 schemas.SubmissionStatus 对应） */
export interface SubmissionStatus {
  request_id: string;
  status: "pending" | "processing" | "succeeded" | "failed";
  attempts: number;
  error: { type: string; message: string } | null;
  created_at: string;
  updated_at: string;
}

/** 当前挂起点 */
export type CurrentStep = "review" | "leader_decision" | "done" | "";

/** 审批链节点 */
export interface ApprovalChainNode {
  role: string;
  id: string;
  name: string;
  dept: string;
  email: string;
}

/** 合规检查项 */
export interface ComplianceCheck {
  item: string;
  passed: boolean;
  detail: string;
}

/** 制度条款依据（RAG 检索命中） */
export interface PolicyBasisHit {
  clause_id: string;
  source: string;
  text: string;
  score: number;
}

/** 事前申请单 */
export interface AdvanceApplication {
  app_id: string;
  employee_id: string;
  direction: string;
  start_date: string;
  end_date: string;
  valid_until: string;
  estimated_amount: number;
  purpose: string;
  status: string;
  created_at: string;
  /** 已占用合计（approved 报销单按票面累计；列表/匹配时附带） */
  reserved_amount?: number;
  /** 剩余额度 = 预估 - 已占用（可为负=超支；列表附带，详情/匹配结果可自行算） */
  remaining_amount?: number;
}

/** 票面信息 */
export interface InvoiceData {
  invoice_no: string;
  invoice_type: string;
  date: string;
  amount: number;
  title: string;
  risk_flags?: string[];
}

/** #A 批内被接受票（同一报销单内逐票视图：票面 + 验真 + 逐票软闸门结果） */
export interface BatchTicket {
  /** 该票上传 meta（file_name 原始文件名；object_key=对象存储源文件 key，#93 原件取用凭据） */
  invoice_input: {
    employee_id?: string;
    file_name?: string;
    declared_amount?: number;
    /** #93 对象存储取用接线：原件下载/预览端点凭据（服务端已摘除 file_path，只暴露对象 key） */
    object_key?: string;
    [k: string]: unknown;
  };
  invoice_data: InvoiceData;
  verification: { verified: boolean; duplicate: boolean; note: string };
  /** 逐票软闸门（如 酒店单晚限额/席别越级）——请求级结论看顶层 compliance_checks */
  compliance_checks: ComplianceCheck[];
}

/** #A 批内被拒票（不进审批、不入池；category/message/suggestion 即重传依据） */
export interface RejectedTicket {
  invoice_no: string;
  invoice_type: string;
  amount: number | null;
  file_name: string;
  category: string;
  message: string;
  suggestion: string;
  /** #93 被拒票也保留上传 meta（object_key 可取原件核对，重传依据不只文字） */
  invoice_input?: { file_name?: string; object_key?: string; [k: string]: unknown };
}

/** 审批记录 */
export interface ApprovalRecord {
  role: string;
  decision: string;
  actor: string;
  comment: string;
  time: string;
}

/** 退回原因 */
export interface ReturnReason {
  category: string;
  message: string;
  suggestion: string;
}

/** 站内消息留痕 */
export interface MessageRecord {
  id: number;
  request_id: string;
  to_role: string;
  content: string;
  created_at: string;
}

/** 报销单详情（提交/审批/查询通用返回） */
export interface RequestDetail {
  request_id: string;
  status: ProcessStatus;
  current_step: CurrentStep;
  business_type: string;
  summary: string;
  invoice_data: InvoiceData | null;
  /** #A 多票批：被接受并入审的票列表（单票请求=长度 1；老数据（无 tickets）为 null → 回退单票视图） */
  tickets?: BatchTicket[] | null;
  /** #A 多票批：被拒票列表（不进审批，供报销人按票重传/核对原因） */
  rejected?: RejectedTicket[] | null;
  /** #A 多票批：Σ 被接受票面金额（审批链档位/预算占用/通知金额口径；单票=票面金额） */
  total_amount?: number | null;
  verification: { verified: boolean; duplicate: boolean; note: string } | null;
  advance_application: AdvanceApplication | null;
  compliance_checks: ComplianceCheck[] | null;
  policy_basis?: PolicyBasisHit[];
  approval_chain: ApprovalChainNode[] | null;
  return_reason: ReturnReason | null;
  approval_records: ApprovalRecord[] | null;
  decision: { action: string; comment: string; actor: string } | null;
  /** 打款记录（出纳确认打款后产生）：{ actor: 出纳工号, comment: 备注, time } */
  payment: { actor: string; comment: string; time: string } | null;
  paused: boolean;
  messages: MessageRecord[];
  emails: { id: number; request_id: string; to: string; subject: string; body: string; created_at: string }[];
}

/** 报销单列表项 */
export interface RequestSummary {
  request_id: string;
  status: ProcessStatus;
  current_step: CurrentStep;
  business_type: string;
  amount: number | null;
  employee_id: string;
  created_at: string;
  updated_at: string;
}
