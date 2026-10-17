import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tarvo | Fechamento Financeiro, Governança & Rastro",
  description: "Dashboard executivo da Tarvo: comissões, notas, divergências de taxa, conciliação bancária e explicador de rastro.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="pt-BR">
      <body>{children}</body>
    </html>
  );
}
