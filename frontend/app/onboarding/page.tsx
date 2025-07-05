import OnboardingForm from '@/components/OnboardingForm';

export default function OnboardingPage() {
  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 py-12">
      <h1 className="text-center text-3xl font-bold mb-8 text-gray-900 dark:text-gray-100">
        Onboarding
      </h1>
      <OnboardingForm />
    </div>
  );
}