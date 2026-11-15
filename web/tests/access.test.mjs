import {test} from 'node:test';
import assert from 'node:assert/strict';
import {canViewAdminNavigation} from '../lib/access.mjs';

test('administration navigation requires an approved owner or administrator', () => {
  assert.equal(canViewAdminNavigation({role:'owner',approved:true}),true);
  assert.equal(canViewAdminNavigation({role:'admin',approved:true}),true);
  assert.equal(canViewAdminNavigation({role:'admin',approved:false}),false);
  assert.equal(canViewAdminNavigation({role:'user',approved:true}),false);
  assert.equal(canViewAdminNavigation({role:'user',approved:false}),false);
  assert.equal(canViewAdminNavigation(null),false);
});
