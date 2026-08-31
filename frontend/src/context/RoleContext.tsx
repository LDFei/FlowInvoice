// 角色上下文 —— 顶部切换角色，模拟"登录态"
// 业务：Demo 无登录/SSO，用角色下拉模拟登录；真实系统由登录态注入 employee_id
// 关键：审批中心按 角色×步骤 渲染可操作按钮，与后端 _authorize 权限校验一致
import { createContext, useContext, useState, type ReactNode } from "react";

export interface RoleDef {
  id: string;
  name: string;
  label: string;
  canReview: boolean; // 是否为审核人（review 步骤可操作）
  canLead: boolean; // 是否为总经理/领导（leader_decision 步骤可操作，大额>2000 才需终审）
  canPay: boolean; // 是否为出纳/财务（approved 后执行打款，审批≠支付）
}

export const ROLES: RoleDef[] = [
  { id: "1001", name: "张三", label: "报销人", canReview: false, canLead: false, canPay: false },
  { id: "2001", name: "李四", label: "审核人 · 直属上级", canReview: true, canLead: false, canPay: false },
  { id: "3001", name: "赵六", label: "出纳 · 财务", canReview: false, canLead: false, canPay: true },
  { id: "4001", name: "孙七", label: "总经理 · 最终审批", canReview: false, canLead: true, canPay: false },
];

interface RoleContextValue {
  role: RoleDef;
  setRole: (r: RoleDef) => void;
}

const RoleContext = createContext<RoleContextValue | null>(null);

export function RoleProvider({ children }: { children: ReactNode }) {
  const [role, setRole] = useState<RoleDef>(ROLES[0]);
  return <RoleContext.Provider value={{ role, setRole }}>{children}</RoleContext.Provider>;
}

export function useRole(): RoleContextValue {
  const ctx = useContext(RoleContext);
  if (!ctx) throw new Error("useRole 必须在 <RoleProvider> 内使用");
  return ctx;
}
