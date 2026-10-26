param(
  [int]$Top = 3,
  [int]$WeeklyTop = 15,
  [switch]$Experiment,
  [int]$Rank = 1
)

$ArgsList = @("tools/pipeline.py", "--top", "$Top", "--weekly-top", "$WeeklyTop")
if ($Experiment) { $ArgsList += @("--experiment", "--rank", "$Rank") }

python @ArgsList
