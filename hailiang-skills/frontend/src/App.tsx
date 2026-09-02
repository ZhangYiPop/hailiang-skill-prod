import { lazy, Suspense } from "react";
import { BrowserRouter as Router, Route, Routes } from "react-router-dom";

import Home from "@/pages/Home";

const Workbench = lazy(() => import("@/pages/Workbench"));

export default function App() {
  return (
    <Router>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/workbench" element={<Suspense fallback={<main className="grid min-h-screen place-items-center bg-[#07101d] text-slate-300">正在加载业务调试台…</main>}><Workbench /></Suspense>} />
      </Routes>
    </Router>
  );
}
