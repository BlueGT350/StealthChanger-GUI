/* ktc-gui: minimal Moonraker JSON-RPC websocket client (no dependencies). */
(function (KTC) {
  'use strict';
  function Rpc(url) {
    this.url = url; this.id = 0; this.pending = {}; this.handlers = {};
    this.ws = null; this.retry = 1000; this.closedByUser = false;
  }
  Rpc.prototype.on = function (method, cb) { (this.handlers[method] = this.handlers[method] || []).push(cb); };
  Rpc.prototype.emit = function (method, params) {
    (this.handlers[method] || []).forEach(function (cb) { try { cb(params); } catch (e) { console.error(e); } });
  };
  Rpc.prototype.connect = function () {
    var self = this;
    try { this.ws = new WebSocket(this.url); } catch (e) { this.emit('_error', e); this._reconnect(); return; }
    this.ws.onopen = function () { self.retry = 1000; self.emit('_open'); };
    this.ws.onclose = function () {
      Object.keys(self.pending).forEach(function (k) { self.pending[k].reject(new Error('connection closed')); });
      self.pending = {};
      self.emit('_close');
      if (!self.closedByUser) self._reconnect();
    };
    this.ws.onmessage = function (ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.id !== undefined && self.pending[msg.id]) {
        var p = self.pending[msg.id]; delete self.pending[msg.id];
        if (msg.error) { var err = new Error(msg.error.message); err.code = msg.error.code; p.reject(err); }
        else p.resolve(msg.result);
      } else if (msg.method) {
        self.emit(msg.method, msg.params || []);
      }
    };
  };
  Rpc.prototype._reconnect = function () {
    var self = this;
    setTimeout(function () { self.connect(); }, this.retry);
    this.retry = Math.min(this.retry * 2, 10000);
  };
  Rpc.prototype.call = function (method, params) {
    var self = this;
    return new Promise(function (resolve, reject) {
      if (!self.ws || self.ws.readyState !== 1) { reject(new Error('not connected')); return; }
      var id = ++self.id;
      self.pending[id] = { resolve: resolve, reject: reject };
      self.ws.send(JSON.stringify({ jsonrpc: '2.0', method: method, params: params || {}, id: id }));
    });
  };
  KTC.Rpc = Rpc;
})(window.KTC = window.KTC || {});
