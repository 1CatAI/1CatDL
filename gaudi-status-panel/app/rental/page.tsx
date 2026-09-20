import { RentalPanel } from '@/components/rental/rental-panel';

export const metadata = { title: '1CatDL · 算力工作台', description: 'G2-002 Gaudi2 单卡实例、余额账单与客户管理' };

export const dynamic = 'force-dynamic';

export default function RentalPage() {
  return <RentalPanel />;
}
