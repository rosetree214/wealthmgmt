import { api } from '@/lib/api';

async function getPortfolio() {
  try {
    return await api<{ portfolio: any[] }>('/api/portfolio');
  } catch (e) {
    console.error(e);
    return { portfolio: [] };
  }
}

export default async function PortfolioPage() {
  const { portfolio } = await getPortfolio();
  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 p-8">
      <h1 className="text-3xl font-bold mb-6 text-gray-900 dark:text-gray-100">
        Portfolio Overview
      </h1>
      {portfolio.length === 0 ? (
        <p>No holdings yet.</p>
      ) : (
        <ul className="space-y-4">
          {portfolio.map((item: any) => (
            <li key={item.id} className="p-4 bg-white dark:bg-gray-800 rounded shadow">
              {JSON.stringify(item)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}