import { expect, test } from 'claude-code/testing'
import { localTime } from '../hooks/time'

test('naive task timestamps are UTC and the local offset follows each date across DST', () => {
  const zone = 'Europe/Amsterdam'
  expect(localTime('2026-10-24T13:15:00.968123', zone)).toBe('15:15')
  expect(localTime('2026-10-26T13:15:00.968123', zone)).toBe('14:15')
  expect(localTime('2026-10-29T13:15:00.968123', zone)).toBe('14:15')
  expect(localTime('2026-10-25T00:30:00.123456Z', zone)).toBe('02:30')
  expect(localTime('2026-10-25T01:30:00.123456Z', zone)).toBe('02:30')
  for (const at of ['2026-10-24T13:15:00.968123', '2026-10-26T13:15:00.968123']) {
    expect(localTime(at)).toBe(localTime(`${at}+00:00`))
    expect(localTime(`${at}Z`, zone)).toBe(localTime(at, zone))
  }
  expect(localTime('not a timestamp', zone)).toBe('')
})
