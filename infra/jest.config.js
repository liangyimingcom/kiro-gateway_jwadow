/**
 * Jest 配置 — 供后续 CDK 快照与合规测试使用（任务 14.3 / 15.5）。
 */
module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest',
  },
};
