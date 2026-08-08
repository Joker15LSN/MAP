interface ScenePanelProps {
  sceneResult?: Record<string, unknown>;
}

export const ScenePanel = ({ sceneResult }: ScenePanelProps) => (
  <pre className="raw-json">{JSON.stringify(sceneResult || {}, null, 2)}</pre>
);
