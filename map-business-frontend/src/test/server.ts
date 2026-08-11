import { setupServer } from 'msw/node';
import { handlers } from './handlers';

/** Node 环境下 MSW mock server（FIX-P2-FRONTEND-01 测试底座）。 */
export const server = setupServer(...handlers);
