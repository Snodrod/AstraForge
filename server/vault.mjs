import { spawn } from 'node:child_process';
import path from 'node:path';
import { data,readJson,writeJson } from './storage.mjs';
const file=path.join(data,'vault.json');
const scripts={encrypt:'$s=[Console]::In.ReadToEnd(); ConvertFrom-SecureString (ConvertTo-SecureString $s -AsPlainText -Force)',decrypt:'$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}'};
function dpapi(mode,input){return new Promise((resolve,reject)=>{const p=spawn('powershell.exe',['-NoProfile','-NonInteractive','-Command',scripts[mode]],{windowsHide:true});let result='';p.stdout.on('data',v=>result+=v);p.on('error',reject);p.on('exit',code=>code===0?resolve(result.trim()):reject(new Error('Failed to open the protected Windows store.')));p.stdin.end(input);});}
export async function setKey(provider,key){if(process.platform!=='win32')throw new Error('On this OS, set the key through an environment variable.');const vault=await readJson(file,{});if(key) vault[provider]=await dpapi('encrypt',key);else delete vault[provider];await writeJson(file,vault);}
export async function getKey(provider){const env=process.env[{openai:'OPENAI_API_KEY',anthropic:'ANTHROPIC_API_KEY',tripo:'TRIPO_API_KEY'}[provider]];if(env)return env;const vault=await readJson(file,{});return vault[provider]?dpapi('decrypt',vault[provider]):null;}
export async function keyStatus(){const vault=await readJson(file,{});return {openai:!!(process.env.OPENAI_API_KEY||vault.openai),anthropic:!!(process.env.ANTHROPIC_API_KEY||vault.anthropic),tripo:!!(process.env.TRIPO_API_KEY||vault.tripo)};}
