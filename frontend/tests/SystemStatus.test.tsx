// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SystemStatus } from '../src/components/SystemStatus';

afterEach(cleanup);
it('shows gate errors, model information, and refresh without claiming service health', () => {
  const refresh = vi.fn();
  render(<SystemStatus gate={undefined} gateError="Gate 연결 실패" loading={false} models={['model/a']} modelsUnavailable={false} onRefresh={refresh} onSwitchSucceeded={vi.fn()} />);
  expect(screen.getByRole('alert').textContent).toBe('Gate 연결 실패');
  expect(screen.getByText('model/a')).toBeTruthy();
  expect(screen.getByText('1개')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '새로고침' }));
  expect(refresh).toHaveBeenCalledOnce();
});
