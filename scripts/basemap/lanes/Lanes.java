package com.protomaps.basemap.layers;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureMerge;
import com.onthegomap.planetiler.ForwardingProfile;
import com.onthegomap.planetiler.VectorTile;
import com.onthegomap.planetiler.geo.GeoUtils;
import com.onthegomap.planetiler.geo.GeometryException;
import com.onthegomap.planetiler.reader.SourceFeature;
import java.util.*;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.LineString;
import org.locationtech.jts.operation.buffer.OffsetCurve;

/**
 * Touror 車道級圖層（加在 Protomaps basemap 上，不動既有圖層）：
 * lanes（每條道路的車道屬性，含推定）、lane_lines（預先切好的車道中心線與分隔線，z14 才有，App 端 minzoom 15 起顯示）、
 * road_areas（area:highway 等路面多邊形）、islands_pt（分隔島／行人庇護島點）。
 * 座標：planetiler world 座標 y 向下，所以 JTS 正 offset（數學左側）＝地理右側。
 */
public class Lanes implements ForwardingProfile.LayerPostProcessor {

  public static final String LAYER_NAME = "lanes";
  private static final double EARTH_M = 40075016.686;

  private static final Set<String> VEHICLE = Set.of("motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "road", "track", "busway");
  private static final Set<String> MAJOR = Set.of("motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link");

  /** 政府車道屬性（資料/車道/gov_lane_attrs.csv，以 OSM way id 對應）；檔案不存在就是空表。 */
  record Gov(Integer lanes, Integer fwd, Integer bwd, String width, String confidence, String source,
    String motoPriority) {}

  private final Map<Long, Gov> gov = new HashMap<>();

  public Lanes() {
    String path = System.getProperty("touror.govLanes", "data/sources/gov_lane_attrs.csv");
    var f = java.nio.file.Path.of(path);
    if (!java.nio.file.Files.exists(f)) return;
    try (var lines = java.nio.file.Files.lines(f)) {
      var it = lines.iterator();
      String[] head = it.next().split(",", -1);
      Map<String, Integer> col = new HashMap<>();
      for (int i = 0; i < head.length; i++) col.put(head[i].trim(), i);
      while (it.hasNext()) {
        String[] c = it.next().split(",", -1);
        if (c.length < head.length) continue;
        long id = Long.parseLong(c[col.get("osm_way_id")]);
        if (id >= 9_100_000_000_000L) id = 2_000_000_000L + (id - 9_100_000_000_000L); // 與 renumber_synthetic.py 一致
        String w = c[col.get("width")];
        gov.put(id, new Gov(parseInt(c[col.get("lanes")]), parseInt(c[col.get("lanes_forward")]),
          parseInt(c[col.get("lanes_backward")]), w.isEmpty() ? null : w, c[col.get("confidence")],
          c[col.get("source")], c[col.get("moto_priority_lane")].isEmpty() ? null : c[col.get("moto_priority_lane")]));
      }
    } catch (Exception e) {
      System.err.println("gov_lane_attrs.csv 讀取失敗，略過：" + e);
      gov.clear();
    }
    System.err.println("Touror lanes：政府車道屬性 " + gov.size() + " 筆");
    loadMapillary(System.getProperty("touror.mapillaryLanes", "data/sources/mapillary_ai_lanes.csv"));
  }

  /** Mapillary 照片 AI 判讀車道（data/lanes/mapillary_ai_lanes.csv，CC BY-SA 4.0）：以 way id＋方向對應。 */
  record Mly(Integer fwd, Integer bwd, String turnF, String turnB) {}

  private final Map<Long, Mly> mly = new HashMap<>();

  private void loadMapillary(String path) {
    var f = java.nio.file.Path.of(path);
    if (!java.nio.file.Files.exists(f)) return;
    try (var lines = java.nio.file.Files.lines(f)) {
      var it = lines.iterator();
      String[] head = it.next().split(",", -1);
      Map<String, Integer> col = new HashMap<>();
      for (int i = 0; i < head.length; i++) col.put(head[i].trim(), i);
      while (it.hasNext()) {
        String[] c = it.next().split(",", head.length);
        if (c.length < 4) continue;
        long id = Long.parseLong(c[col.get("osm_way_id")].trim());
        boolean back = "backward".equals(c[col.get("direction")].trim());
        Integer n = parseInt(c[col.get("lanes")]);
        String t = c[col.get("turn_lanes")].trim();
        if (t.isEmpty() || t.split("\\|", -1).length != (n == null ? -1 : n)) t = null;
        Mly old = mly.getOrDefault(id, new Mly(null, null, null, null));
        mly.put(id, back ? new Mly(old.fwd(), n, old.turnF(), t) : new Mly(n, old.bwd(), t, old.turnB()));
      }
    } catch (Exception e) {
      System.err.println("mapillary_ai_lanes.csv 讀取失敗，略過：" + e);
      mly.clear();
    }
    System.err.println("Touror lanes：Mapillary AI 判讀車道 " + mly.size() + " 條 way");
  }

  @Override
  public String name() {
    return LAYER_NAME;
  }

  /** 推定車道數（無 lanes 標籤時）：{單向, 雙向合計}。 */
  static int[] inferredLanes(String hw) {
    return switch (hw) {
      case "motorway", "trunk", "primary" -> new int[]{2, 4};
      case "secondary" -> new int[]{2, 2};
      case "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link" -> new int[]{1, 2};
      case "tertiary", "unclassified", "residential", "living_street", "road", "busway" -> new int[]{1, 2};
      default -> new int[]{1, 1}; // service, track
    };
  }

  static double defaultLaneWidth(String hw) {
    return switch (hw) {
      case "motorway", "motorway_link", "trunk", "trunk_link" -> 3.75;
      case "primary", "primary_link", "secondary", "secondary_link" -> 3.5;
      default -> 3.0;
    };
  }

  static Integer parseInt(String s) {
    if (s == null) return null;
    try {
      int v = (int) Math.round(Double.parseDouble(s.trim().split(";")[0]));
      return v > 0 && v <= 16 ? v : null;
    } catch (NumberFormatException e) {
      return null;
    }
  }

  static Double parseWidth(String s) {
    if (s == null) return null;
    try {
      return Double.parseDouble(s.trim().replace(" m", "").replace("m", ""));
    } catch (NumberFormatException e) {
      return null;
    }
  }

  static String[] split(String s) {
    return s == null ? null : s.split("\\|", -1);
  }

  static String at(String[] arr, int i) {
    if (arr == null || i >= arr.length) return null;
    String v = arr[i].trim();
    return v.isEmpty() ? "none" : v;
  }

  public void processOsm(SourceFeature sf, FeatureCollector features) {
    // ---- islands_pt ----
    if (sf.isPoint()) {
      String kind = null;
      if (sf.hasTag("crossing:island", "yes")) kind = "crossing_island";
      else if (sf.hasTag("traffic_calming", "island")) kind = "traffic_island";
      else if (sf.hasTag("area:highway", "traffic_island")) kind = "traffic_island";
      if (kind != null) {
        features.point("islands_pt").setAttr("kind", kind).setAttr("highway", sf.getString("highway"))
          .setMinZoom(14);
      }
      return;
    }
    // ---- road_areas ----
    String areaHw = sf.getString("area:highway");
    if (sf.canBePolygon() && (areaHw != null || (sf.hasTag("highway") && sf.hasTag("area", "yes")) ||
      sf.hasTag("traffic_calming", "island"))) {
      String kind = areaHw != null ? areaHw : sf.hasTag("traffic_calming", "island") ? "traffic_island" :
        sf.getString("highway");
      features.polygon("road_areas").setAttr("kind", kind).setAttr("highway", sf.getString("highway"))
        .setMinZoom(14).setPixelTolerance(0);
      return;
    }
    // ---- lanes / lane_lines ----
    String hw = sf.getString("highway");
    if (hw == null || !VEHICLE.contains(hw) || !sf.canBeLine() || sf.hasTag("area", "yes")) return;

    String ow = sf.getString("oneway");
    boolean reverse = "-1".equals(ow);
    boolean oneway = reverse || "yes".equals(ow) || "1".equals(ow) || "true".equals(ow) ||
      (ow == null && (hw.equals("motorway") || sf.hasTag("junction", "roundabout", "circular")));
    String onewayAttr = reverse ? "-1" : oneway ? "yes" : "no";

    Integer lanesTag = parseInt(sf.getString("lanes"));
    Integer fwdTag = parseInt(sf.getString("lanes:forward"));
    Integer bwdTag = parseInt(sf.getString("lanes:backward"));
    String turn = sf.getString("turn:lanes");
    String turnF = sf.getString("turn:lanes:forward");
    String turnB = sf.getString("turn:lanes:backward");
    boolean osm = lanesTag != null || fwdTag != null;
    // OSM 沒標車道數時才用政府資料（confidence=low 不用）；OSM 優先
    Gov g = gov.get(sf.id());
    boolean useGov = false;
    String widthStr = sf.getString("width");
    if (g != null && !"low".equals(g.confidence())) {
      if (!osm && (g.lanes() != null || g.fwd() != null)) {
        lanesTag = g.lanes();
        fwdTag = g.fwd();
        bwdTag = g.bwd();
        useGov = true;
      }
      if (widthStr == null) widthStr = g.width();
    }
    // 優先序 osm > gov > mapillary_ai > inferred；AI 判讀的 turn_lanes 只在 OSM 該方向無值、且車道數相符時採用
    Mly m = mly.get(sf.id());
    boolean useMly = false;
    if (m != null && !osm && !useGov) {
      if (oneway) {
        Integer v = reverse ? m.bwd() : m.fwd();
        if (v != null) {
          lanesTag = v;
          fwdTag = null;
          bwdTag = null;
          useMly = true;
        }
      } else if (m.fwd() != null && m.bwd() != null) {
        // 雙向道路：兩個方向都有判讀才採用；只判讀一個方向時不以推定值一半補另一方向，維持原來源（inferred）
        fwdTag = m.fwd();
        bwdTag = m.bwd();
        lanesTag = fwdTag + bwdTag;
        useMly = true;
      }
    }
    String source = osm ? "osm" : useGov ? "gov" : useMly ? "mapillary_ai" : "inferred";
    int total;
    if (lanesTag != null) total = lanesTag;
    else if (fwdTag != null) total = fwdTag + (bwdTag != null ? bwdTag : oneway ? 0 : 1);
    else {
      int[] inf = inferredLanes(hw);
      total = oneway ? inf[0] : inf[1];
    }
    int fwd, bwd;
    if (oneway) {
      fwd = total;
      bwd = 0;
    } else if (fwdTag != null && bwdTag != null) {
      fwd = fwdTag;
      bwd = bwdTag;
    } else if (fwdTag != null) {
      fwd = fwdTag;
      bwd = Math.max(0, total - fwdTag);
    } else if (bwdTag != null) {
      bwd = bwdTag;
      fwd = Math.max(0, total - bwdTag);
    } else {
      bwd = total / 2;
      fwd = total - bwd;
    }

    String turnSource = turn != null || turnF != null || turnB != null ? "osm" : null;
    if (m != null) {
      if (oneway && turn == null && turnF == null && turnB == null) {
        String t = reverse ? m.turnB() : m.turnF();
        if (t != null && t.split("\\|", -1).length == total) {
          turn = t;
          turnSource = "mapillary_ai";
        }
      } else if (!oneway) {
        if (turnF == null && m.turnF() != null && m.turnF().split("\\|", -1).length == fwd) {
          turnF = m.turnF();
          turnSource = turnSource == null ? "mapillary_ai" : turnSource;
        }
        if (turnB == null && m.turnB() != null && m.turnB().split("\\|", -1).length == bwd) {
          turnB = m.turnB();
          turnSource = turnSource == null ? "mapillary_ai" : turnSource;
        }
      }
    }

    int minZoom = MAJOR.contains(hw) ? 12 : 14;
    var f = features.line(LAYER_NAME)
      .setAttr("highway", hw)
      .setAttr("name", sf.getString("name"))
      .setAttr("lanes", total)
      .setAttr("lanes_forward", oneway ? null : fwd)
      .setAttr("lanes_backward", oneway ? null : bwd)
      .setAttr("lanes_source", source)
      .setAttr("oneway", onewayAttr)
      .setAttr("turn_lanes", turn)
      .setAttr("turn_lanes_forward", turnF)
      .setAttr("turn_lanes_backward", turnB)
      .setAttr("turn_lanes_source", turnSource)
      .setAttr("change_lanes", sf.getString("change:lanes"))
      .setAttr("width", widthStr)
      .setAttr("moto_priority_lane", g == null ? null : g.motoPriority())
      .setAttr("divider", sf.getString("divider"))
      .setAttr("motorcycle_lanes", sf.getString("motorcycle:lanes"))
      .setAttr("moped_lanes", sf.getString("moped:lanes"))
      .setAttr("placement", sf.getString("placement"))
      .setAttr("shoulder", sf.getString("shoulder"))
      .setMinZoom(minZoom)
      .setPixelTolerance(0);

    if (!osm && !useGov && !useMly) return; // lane_lines 只給 OSM／政府資料／AI 判讀有車道數的路（lanes_source 區分）
    LineString base;
    try {
      base = (LineString) sf.line();
    } catch (GeometryException | ClassCastException e) {
      return;
    }
    if (reverse) base = base.reverse();
    double lat = GeoUtils.getWorldLat(base.getCentroid().getY());
    double mPerUnit = EARTH_M * Math.cos(Math.toRadians(lat));

    double w = defaultLaneWidth(hw);
    Double width = parseWidth(widthStr);
    if (width != null && total > 0 && width / total >= 2.5 && width / total <= 5.0) w = width / total;

    String[] turnArr = split(oneway ? (turn != null ? turn : turnF) : turnF);
    String[] turnBArr = split(turnB);
    String[] chg = split(oneway ? sf.getString("change:lanes") : sf.getString("change:lanes:forward"));
    String[] mc = split(oneway ? sf.getString("motorcycle:lanes") : sf.getString("motorcycle:lanes:forward"));
    String[] mcB = split(sf.getString("motorcycle:lanes:backward"));

    if (oneway) {
      // 車道 i（0＝行進方向最左）中心在「右側」偏移 (i - (n-1)/2)*w
      double shift = 0;
      String pl = sf.getString("placement");
      if (pl != null && pl.contains(":")) {
        String[] p = pl.split(":");
        Integer k = parseInt(p[1]);
        if (k != null && k <= fwd) {
          double c = (k - 1 - (fwd - 1) / 2.0) * w;
          shift = switch (p[0]) {
            case "left_of" -> c - w / 2;
            case "right_of" -> c + w / 2;
            case "middle_of" -> c;
            default -> 0;
          };
        }
      }
      for (int i = 0; i < fwd; i++) {
        double off = (i - (fwd - 1) / 2.0) * w - shift;
        var lo = emitLane(features, base, mPerUnit, off, i, fwd, "forward", at(turnArr, i), at(mc, i), hw, false);
        if (lo != null) lo.setAttr("lanes_source", source);
      }
      for (int k = 0; k <= fwd; k++) {
        double off = (k - fwd / 2.0) * w - shift;
        String type = (k == 0 || k == fwd) ? "edge" : boundaryType(chg, k - 1);
        emitLine(features, base, mPerUnit, off, k, "forward", type, hw);
      }
    } else {
      // 正向車道在右（正 offset），反向車道在左；0＝各自行進方向最左（最靠中線）
      for (int i = 0; i < fwd; i++) {
        var lf = emitLane(features, base, mPerUnit, (i + 0.5) * w, i, fwd, "forward", at(turnArr, i), at(mc, i), hw, false);
        if (lf != null) lf.setAttr("lanes_source", source);
      }
      for (int i = 0; i < bwd; i++) {
        var lb = emitLane(features, base, mPerUnit, -(i + 0.5) * w, i, bwd, "backward", at(turnBArr, i), at(mcB, i),
          hw, true);
        if (lb != null) lb.setAttr("lanes_source", source);
      }
      if (fwd > 0 && bwd > 0) {
        var c = emitLine(features, base, mPerUnit, 0, 0, "both", "center", hw);
        if (c != null) c.setAttr("divider", sf.getString("divider"));
      }
      for (int k = 1; k <= fwd; k++) {
        emitLine(features, base, mPerUnit, k * w, k, "forward", k == fwd ? "edge" : boundaryType(chg, k - 1), hw);
      }
      String[] chgB = split(sf.getString("change:lanes:backward"));
      for (int k = 1; k <= bwd; k++) {
        emitLine(features, base, mPerUnit, -k * w, k, "backward", k == bwd ? "edge" : boundaryType(chgB, k - 1), hw);
      }
    }
  }

  /** 車道 i 與 i+1 之間：任一側禁止變換到另一側 → 實線。 */
  static String boundaryType(String[] chg, int i) {
    if (chg == null || i + 1 >= chg.length) return "dashed";
    String a = chg[i].trim(), b = chg[i + 1].trim();
    boolean solid = a.equals("no") || a.equals("not_right") || b.equals("no") || b.equals("not_left");
    return solid ? "solid" : "dashed";
  }

  /** offM：相對 OSM 線（已依 oneway=-1 反轉）行進方向的右側距離（公尺），負＝左側。 */
  private static Geometry offset(LineString base, double mPerUnit, double offM) {
    if (Math.abs(offM) < 1e-6) return base;
    Geometry g = OffsetCurve.getCurve(base, offM / mPerUnit);
    return g == null || g.isEmpty() ? null : g;
  }

  private static FeatureCollector.Feature emitLane(FeatureCollector features, LineString base, double mPerUnit, double offM, int idx,
    int count, String side, String turn, String moto, String hw, boolean reverse) {
    Geometry g = offset(base, mPerUnit, offM);
    if (g == null) return null;
    if (reverse) g = g.reverse();
    return features.geometry("lane_lines", g)
      .setAttr("kind", "lane")
      .setAttr("lane_index", idx)
      .setAttr("lane_count", count)
      .setAttr("side", side)
      .setAttr("turn", turn)
      .setAttr("motorcycle", moto)
      .setAttr("offset_m", Math.round(offM * 100) / 100.0)
      .setAttr("highway", hw)
      .setMinZoom(14)
      .setPixelTolerance(0);
  }

  private static FeatureCollector.Feature emitLine(FeatureCollector features, LineString base, double mPerUnit,
    double offM, int idx, String side, String type, String hw) {
    Geometry g = offset(base, mPerUnit, offM);
    if (g == null) return null;
    return features.geometry("lane_lines", g)
      .setAttr("kind", "line")
      .setAttr("line_type", type)
      .setAttr("lane_index", idx)
      .setAttr("side", side)
      .setAttr("offset_m", Math.round(offM * 100) / 100.0)
      .setAttr("highway", hw)
      .setMinZoom(14)
      .setPixelTolerance(0);
  }

  @Override
  public List<VectorTile.Feature> postProcess(int zoom, List<VectorTile.Feature> items) throws GeometryException {
    if (zoom < 14) {
      items = FeatureMerge.mergeLineStrings(items, 0.5, 0.1, 4);
    }
    return items;
  }
}
