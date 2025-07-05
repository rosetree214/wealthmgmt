"use client";

import { useState } from 'react';
import { api } from '@/lib/api';
import { useRouter } from 'next/navigation';

const tiers = [
  { value: 'TIER_10_25', label: '$10M – $25M' },
  { value: 'TIER_25_50', label: '$25M – $50M' },
  { value: 'TIER_50_100', label: '$50M – $100M' },
];

export default function OnboardingForm() {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState({
    tier: 'TIER_10_25',
    riskTolerance: 'moderate',
    liquidityNeeds: '',
    timeHorizon: '10',
    values: '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const router = useRouter();

  const update = (field: string, value: string) =>
    setForm((f) => ({ ...f, [field]: value }));

  const next = () => setStep((s) => s + 1);
  const prev = () => setStep((s) => Math.max(0, s - 1));

  const submit = async () => {
    if (!form.liquidityNeeds || Number(form.liquidityNeeds) <= 0) {
      setError('Liquidity needs must be greater than 0');
      return;
    }
    try {
      setLoading(true);
      await api('/api/user/profile', {
        method: 'POST',
        body: JSON.stringify(form),
      });
      setSuccess(true);
      router.push('/portfolio');
    } catch (e: unknown) {
      if (e instanceof Error) {
        setError(e.message);
      } else {
        setError('An unknown error occurred');
      }
    } finally {
      setLoading(false);
    }
  };

  if (success) return <p className="text-green-600">Profile saved! 🎉</p>;

  return (
    <div className="max-w-xl mx-auto p-6 bg-white dark:bg-gray-900 rounded shadow">
      {error && <p className="text-red-500 mb-4">{error}</p>}

      {step === 0 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold">Net Worth Tier</h2>
          {tiers.map((t) => (
            <label key={t.value} className="block">
              <input
                type="radio"
                name="tier"
                value={t.value}
                checked={form.tier === t.value}
                onChange={(e) => update('tier', e.target.value)}
                className="mr-2"
              />
              {t.label}
            </label>
          ))}
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold">Risk Tolerance</h2>
          <select
            value={form.riskTolerance}
            onChange={(e) => update('riskTolerance', e.target.value)}
            className="w-full border rounded p-2 dark:bg-gray-800"
          >
            <option value="low">Low</option>
            <option value="moderate">Moderate</option>
            <option value="high">High</option>
          </select>

          <h2 className="text-xl font-semibold">Liquidity Needs (in months)</h2>
          <input
            type="number"
            value={form.liquidityNeeds}
            onChange={(e) => update('liquidityNeeds', e.target.value)}
            className="w-full border rounded p-2 dark:bg-gray-800"
          />
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold">Time Horizon (years)</h2>
          <input
            type="number"
            value={form.timeHorizon}
            onChange={(e) => update('timeHorizon', e.target.value)}
            className="w-full border rounded p-2 dark:bg-gray-800"
          />

          <h2 className="text-xl font-semibold">Values & Focus Areas</h2>
          <textarea
            value={form.values}
            onChange={(e) => update('values', e.target.value)}
            className="w-full border rounded p-2 dark:bg-gray-800"
          />
        </div>
      )}

      <div className="flex justify-between mt-6">
        {step > 0 && (
          <button onClick={prev} className="px-4 py-2 rounded bg-gray-200 dark:bg-gray-700">
            Back
          </button>
        )}
        {step < 2 && (
          <button onClick={next} className="ml-auto px-4 py-2 rounded bg-blue-600 text-white">
            Next
          </button>
        )}
        {step === 2 && (
          <button
            disabled={loading}
            onClick={submit}
            className="ml-auto px-4 py-2 rounded bg-green-600 text-white disabled:opacity-50"
          >
            {loading ? 'Saving...' : 'Finish'}
          </button>
        )}
      </div>
    </div>
  );
}