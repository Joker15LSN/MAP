import { Alert, Card } from '@agentscope-ai/design';

interface ErrorAlertProps {
  message?: string;
  className?: string;
}

export const ErrorAlert = ({ message, className }: ErrorAlertProps) => {
  if (!message) {
    return null;
  }
  return (
    <Alert type="error" showIcon message={message} className={className} />
  );
};

interface InfoAlertProps {
  message: string;
  className?: string;
}

export const InfoAlert = ({ message, className }: InfoAlertProps) => (
  <Alert type="info" showIcon message={message} className={className} />
);

export const WarningAlert = ({ message, className }: InfoAlertProps) => (
  <Alert type="warning" showIcon message={message} className={className} />
);

interface LoadingCardProps {
  title: string;
  loading: boolean;
  children: React.ReactNode;
  extra?: React.ReactNode;
  className?: string;
}

export const LoadingCard = ({ title, loading, children, extra, className }: LoadingCardProps) => (
  <Card title={title} loading={loading} extra={extra} className={className}>
    {children}
  </Card>
);
