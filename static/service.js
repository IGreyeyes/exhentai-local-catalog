"use strict";
(() => {
  if(location.hash==="#maintenance-panel"){location.replace("/maintenance");return;}
  async function refresh(){
    try{
      const response=await fetch("/api/maintenance");
      if(!response.ok)return;
      const state=await response.json();
      window.catalogMaintenanceActive=Boolean(state.busy&&["apply","restore"].includes(state.kind));
      const button=document.getElementById("search-button"),results=document.getElementById("results");
      if(button)button.disabled=window.catalogMaintenanceActive||Boolean(window.catalogStopped)||results?.getAttribute("aria-busy")==="true";
      window.collectorUI?.updateControls();
    }catch(error){ /* Connection errors are reported by each page's own requests. */ }
  }
  refresh();setInterval(refresh,3000);
})();
