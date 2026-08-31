// 前端入口：挂载 React 应用 + AntD 中文语言包
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

dayjs.locale("zh-cn");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: "#1677ff" } }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
);
