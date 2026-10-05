'use strict';
const { contextBridge, ipcRenderer } = require('electron');
if (process.isMainFrame) {
  contextBridge.exposeInMainWorld('companion', Object.freeze({
    state: () => ipcRenderer.invoke('companion:state'),
    configure: values => ipcRenderer.invoke('companion:configure', values),
    importInstallation: () => ipcRenderer.invoke('companion:import'),
    launch: () => ipcRenderer.invoke('companion:launch'),
    checkUpdate: () => ipcRenderer.invoke('companion:check-update'),
    installUpdate: () => ipcRenderer.invoke('companion:install-update'),
    recover: () => ipcRenderer.invoke('companion:recover'),
    onState: callback => {
      const listener = (_event, value) => callback(value);
      ipcRenderer.on('companion:state-changed', listener);
      return () => ipcRenderer.removeListener('companion:state-changed', listener);
    },
  }));
}
