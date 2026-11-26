package com.protomaps.basemap.layers;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.reader.SourceFeature;

/**
 * Touror：roads 圖層補立體分層屬性（給導航地圖畫高架／匝道／平面上下層）。由 build_basemap.sh 插入 Roads.java 的 OSM 道路處理。
 * <ul>
 *   <li>layer（整數，z12 起）：OSM layer 標籤；沒標時橋＝1、隧道＝-1；0 不寫（缺值＝0）。
 *   <li>lanes（整數，z14 起）：OSM lanes 標籤（只用實際標註，推定值在 lanes 圖層）。
 *   <li>oneway（z14 起）：官方只照抄標籤；這裡補隱含單行（motorway、圓環）沒標時的 "yes"。
 * </ul>
 * is_bridge／is_tunnel／is_link 官方已有，不重複。FeatureMerge.mergeLineStrings 只合併屬性完全相同的線，
 * 所以 layer 不同（或橋／非橋）的線段在 z12 以上不會被併成一條。
 */
public final class TourorRoadAttrs {

  private TourorRoadAttrs() {}

  public static Integer layerOf(String layerTag, boolean bridge, boolean tunnel) {
    if (layerTag != null) {
      try {
        int v = Integer.parseInt(layerTag.trim().split(";")[0].trim());
        if (v >= -5 && v <= 5) return v;
      } catch (NumberFormatException e) {
        // 非整數 layer：照橋／隧道推
      }
    }
    return bridge ? 1 : tunnel ? -1 : 0;
  }

  static Integer lanesOf(String s) {
    if (s == null) return null;
    try {
      int v = (int) Math.round(Double.parseDouble(s.trim().split(";")[0]));
      return v > 0 && v <= 16 ? v : null;
    } catch (NumberFormatException e) {
      return null;
    }
  }

  public static void apply(FeatureCollector.Feature feat, SourceFeature sf) {
    boolean bridge = sf.hasTag("bridge") && !sf.hasTag("bridge", "no");
    boolean tunnel = sf.hasTag("tunnel") && !sf.hasTag("tunnel", "no");
    int layer = layerOf(sf.getString("layer"), bridge, tunnel);
    if (layer != 0) feat.setAttrWithMinzoom("layer", layer, 12);
    Integer lanes = lanesOf(sf.getString("lanes"));
    if (lanes != null) feat.setAttrWithMinzoom("lanes", lanes, 14);
    if (sf.getString("oneway") == null &&
      (sf.hasTag("highway", "motorway") || sf.hasTag("junction", "roundabout", "circular"))) {
      feat.setAttrWithMinzoom("oneway", "yes", 14);
    }
  }
}
