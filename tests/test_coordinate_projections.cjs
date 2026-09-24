const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const data = require('../assets/property-locations.json');
const gcoord = require('../assets/vendor/gcoord-1.0.7.cjs');

test('all browser and document projections agree with the retained source', () => {
  for (const item of Object.values(data.properties)) {
    if (!item.sourcePoint) continue;
    const p = item.sourcePoint;
    for (const [key, crs] of [['wgs84', 'WGS84'], ['gcj02', 'GCJ02']]) {
      const [lng, lat] = gcoord.transform([p.lng, p.lat], gcoord[p.crs], gcoord[crs]);
      assert.ok(Math.abs(lng-item[key].lng)<1e-7, item.name);
      assert.ok(Math.abs(lat-item[key].lat)<1e-7, item.name);
    }
    const [lng, lat] = gcoord.transform([item.gcj02.lng, item.gcj02.lat], gcoord.GCJ02, gcoord.WGS84);
    assert.ok(Math.abs(lng-item.wgs84.lng)<1e-6, item.name);
    assert.ok(Math.abs(lat-item.wgs84.lat)<1e-6, item.name);
  }
});

test('Wanjia Denghuo uses the audited Amap point in the published bundle', () => {
  const context = {window: {}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets/property-locations.js'), 'utf8'), context);
  const published = JSON.parse(JSON.stringify(context.window.HOUSE_LOCATIONS));
  assert.deepEqual(published, data);
  const item = published.properties['project-29'];
  assert.deepEqual(item.gcj02, {lat: 34.203416, lng: 108.855898});
  assert.deepEqual(item.wgs84, {lat: 34.2050128, lng: 108.85128865});
  const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
  assert.match(html, /<script src="assets\/property-locations\.js\?v=/);
  assert.match(html, /item\.lat=location\?\.wgs84\?\.lat\?\?null/);
  assert.match(html, /item\.lng=location\?\.wgs84\?\.lng\?\?null/);
  const legacyItems = html.split('const items=[', 2)[1].split('];', 1)[0];
  assert.doesNotMatch(legacyItems, /\b(?:lat|lng):/);
});
