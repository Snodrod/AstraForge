#!/usr/bin/env node
import path from 'node:path';
import { setReview } from './attempt_store.mjs';

function parse(argv) {
  const result = { attempt: null, status: null, comment: '' };
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!value) throw new Error(`Missing value for ${flag}`);
    if (flag === '--attempt') result.attempt = value;
    else if (flag === '--status') result.status = value;
    else if (flag === '--comment') result.comment = value;
    else throw new Error(`Unknown option: ${flag}`);
  }
  if (!result.attempt || !result.status) throw new Error('--attempt and --status are required.');
  return result;
}

const options = parse(process.argv.slice(2));
const attemptDir = path.resolve(options.attempt);
setReview(attemptDir, options.status, options.comment);
console.log(JSON.stringify({ attempt: attemptDir, status: options.status, comment: options.comment }, null, 2));
