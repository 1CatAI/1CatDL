import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gpuCapacityReason } from '../components/rental/gpu-plans.ts';
const state = (free, cpu=128, memory=500000) => ({ nodes: [{ id:'G2-002', availableCpu:cpu, availableMemoryMB:memory }], slots: Array.from({length:8},(_,i)=>({slot:i+1,nodeId:'G2-002',state:free.includes(i+1)?'available':'running'})) });
test('four card complete groups only',()=> { assert.equal(gpuCapacityReason(state([5,6,7,8]),'G2-002',4),''); assert.notEqual(gpuCapacityReason(state([1,2,7,8]),'G2-002',4),''); });
test('single fits fragmented free pool',()=>assert.equal(gpuCapacityReason(state([3]),'G2-002',1),''));
test('four needs 64 CPUs and 250000 MB',()=> { assert.notEqual(gpuCapacityReason(state([1,2,3,4],63),'G2-002',4),''); assert.notEqual(gpuCapacityReason(state([1,2,3,4],64,249999),'G2-002',4),''); assert.equal(gpuCapacityReason(state([1,2,3,4],64,250000),'G2-002',4),''); });
test('cannot combine nodes',()=> { const s=state([1,2]); s.slots.push({slot:3,nodeId:'G2-003',state:'available'},{slot:4,nodeId:'G2-003',state:'available'}); assert.notEqual(gpuCapacityReason(s,'G2-002',4),''); });
test('legacy missing budget defaults to card check',()=>assert.equal(gpuCapacityReason({slots:[{slot:1,state:'available'}]},undefined,1),''));
